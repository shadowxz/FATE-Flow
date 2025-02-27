#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import typing
from copy import deepcopy

from fate_arch.common import FederatedMode
from fate_arch.common.base_utils import current_timestamp, json_dumps, json_loads

from fate_flow.controller.job_controller import JobController
from fate_flow.db.db_models import DB, Job, Task
from fate_flow.db.job_default_config import JobDefaultConfig
from fate_flow.entity import JobConfigurationBase, RetCode
from fate_flow.entity.run_status import (
    EndStatus, FederatedSchedulingStatusCode, InterruptStatus, JobInheritanceStatus,
    JobStatus, SchedulingStatusCode, StatusSet, TaskStatus,
)
from fate_flow.entity.types import ResourceOperation
from fate_flow.model.sync_model import SyncModel
from fate_flow.operation.job_saver import JobSaver
from fate_flow.operation.job_tracker import Tracker
from fate_flow.scheduler.federated_scheduler import FederatedScheduler
from fate_flow.scheduler.task_scheduler import TaskScheduler
from fate_flow.settings import ENABLE_MODEL_STORE
from fate_flow.utils import detect_utils, job_utils, model_utils, schedule_utils
from fate_flow.utils.config_adapter import JobRuntimeConfigAdapter
from fate_flow.utils.cron import Cron
from fate_flow.utils.log_utils import exception_to_trace_string, schedule_logger


class DAGScheduler(Cron):
    @classmethod
    def submit(cls, submit_job_conf: JobConfigurationBase, job_id: str = None):
        if not job_id:
            job_id = job_utils.generate_job_id()
        submit_result = {
            "job_id": job_id
        }
        schedule_logger(job_id).info(f"submit job, body {submit_job_conf.to_dict()}")
        try:
            dsl = submit_job_conf.dsl
            runtime_conf = deepcopy(submit_job_conf.runtime_conf)
            job_utils.check_job_conf(runtime_conf, dsl)
            job_initiator = runtime_conf["initiator"]
            conf_adapter = JobRuntimeConfigAdapter(runtime_conf)
            common_job_parameters = conf_adapter.get_common_parameters()
            common_job_parameters.roles = runtime_conf["role"]
            common_job_parameters.role_parameters = runtime_conf.get("job_parameters", {}).get("role", {})

            if common_job_parameters.job_type != "predict":
                # generate job model info
                conf_version = schedule_utils.get_conf_version(runtime_conf)
                if conf_version != 2:
                    raise Exception("only the v2 version runtime conf is supported")
                common_job_parameters.model_id = model_utils.gen_model_id(runtime_conf["role"])
                common_job_parameters.model_version = job_id
                train_runtime_conf = {}
            else:
                # check predict job parameters
                detect_utils.check_config(common_job_parameters.to_dict(), ["model_id", "model_version"])

                # get inference dsl from pipeline model as job dsl
                tracker = Tracker(job_id=job_id, role=job_initiator["role"], party_id=job_initiator["party_id"],
                                  model_id=common_job_parameters.model_id, model_version=common_job_parameters.model_version)

                if ENABLE_MODEL_STORE:
                    sync_model = SyncModel(
                        role=tracker.role, party_id=tracker.party_id,
                        model_id=tracker.model_id, model_version=tracker.model_version,
                    )
                    if sync_model.remote_exists():
                        sync_model.download(True)

                if not model_utils.check_if_deployed(
                    role=tracker.role, party_id=tracker.party_id,
                    model_id=tracker.model_id, model_version=tracker.model_version,
                ):
                    raise Exception(f"model has not been deployed yet")

                pipeline_model = tracker.pipelined_model.read_pipeline_model()
                train_runtime_conf = json_loads(pipeline_model.train_runtime_conf)
                dsl = json_loads(pipeline_model.inference_dsl)

            job = Job()
            job.f_job_id = job_id
            job.f_dsl = dsl
            job.f_train_runtime_conf = train_runtime_conf
            job.f_roles = runtime_conf["role"]
            job.f_initiator_role = job_initiator["role"]
            job.f_initiator_party_id = job_initiator["party_id"]
            job.f_role = job_initiator["role"]
            job.f_party_id = job_initiator["party_id"]

            path_dict = job_utils.save_job_conf(job_id=job_id,
                                                role=job.f_initiator_role,
                                                party_id=job.f_initiator_party_id,
                                                dsl=dsl,
                                                runtime_conf=runtime_conf,
                                                runtime_conf_on_party={},
                                                train_runtime_conf=train_runtime_conf,
                                                pipeline_dsl=None)

            if job.f_initiator_party_id not in runtime_conf["role"][job.f_initiator_role]:
                msg = f"initiator party id {job.f_initiator_party_id} not in roles {runtime_conf['role']}"
                schedule_logger(job_id).info(msg)
                raise Exception(msg)

            # create common parameters on initiator
            JobController.create_common_job_parameters(job_id=job.f_job_id, initiator_role=job.f_initiator_role, common_job_parameters=common_job_parameters)
            job.f_runtime_conf = conf_adapter.update_common_parameters(common_parameters=common_job_parameters)
            dsl_parser = schedule_utils.get_job_dsl_parser(dsl=job.f_dsl,
                                                           runtime_conf=job.f_runtime_conf,
                                                           train_runtime_conf=job.f_train_runtime_conf)

            # initiator runtime conf as template
            job.f_runtime_conf_on_party = job.f_runtime_conf.copy()
            job.f_runtime_conf_on_party["job_parameters"] = common_job_parameters.to_dict()

            # inherit job
            job.f_inheritance_info = common_job_parameters.inheritance_info
            job.f_inheritance_status = JobInheritanceStatus.WAITING if common_job_parameters.inheritance_info else JobInheritanceStatus.PASS
            if job.f_inheritance_info:
                inheritance_jobs = JobSaver.query_job(job_id=job.f_inheritance_info.get("job_id"), role=job_initiator["role"], party_id=job_initiator["party_id"])
                inheritance_tasks = JobSaver.query_task(job_id=job.f_inheritance_info.get("job_id"), role=job_initiator["role"], party_id=job_initiator["party_id"], only_latest=True)
                job_utils.check_job_inheritance_parameters(job, inheritance_jobs, inheritance_tasks)

            status_code, response = FederatedScheduler.create_job(job=job)
            if status_code != FederatedSchedulingStatusCode.SUCCESS:
                job.f_status = JobStatus.FAILED
                job.f_tag = "submit_failed"
                FederatedScheduler.sync_job_status(job=job)
                raise Exception("create job failed", response)
            else:
                need_run_components = {}
                for role in response:
                    need_run_components[role] = {}
                    for party, res in response[role].items():
                        need_run_components[role][party] = [name for name, value in response[role][party]["data"]["components"].items() if value["need_run"] is True]
                if common_job_parameters.federated_mode == FederatedMode.MULTIPLE:
                    # create the task holder in db to record information of all participants in the initiator for scheduling
                    for role, party_ids in job.f_roles.items():
                        for party_id in party_ids:
                            if role == job.f_initiator_role and party_id == job.f_initiator_party_id:
                                continue
                            if not need_run_components[role][party_id]:
                                continue
                            JobController.initialize_tasks(job_id=job_id,
                                                           role=role,
                                                           party_id=party_id,
                                                           run_on_this_party=False,
                                                           initiator_role=job.f_initiator_role,
                                                           initiator_party_id=job.f_initiator_party_id,
                                                           job_parameters=common_job_parameters,
                                                           dsl_parser=dsl_parser,
                                                           components=need_run_components[role][party_id],
                                                           runtime_conf=runtime_conf,
                                                           is_scheduler=True)
                job.f_status = JobStatus.WAITING
                status_code, response = FederatedScheduler.sync_job_status(job=job)
                if status_code != FederatedSchedulingStatusCode.SUCCESS:
                    raise Exception(f"set job to waiting status failed: {response}")

            schedule_logger(job_id).info(f"submit job successfully, job id is {job.f_job_id}, model id is {common_job_parameters.model_id}")
            logs_directory = job_utils.get_job_log_directory(job_id)
            result = {
                "code": RetCode.SUCCESS,
                "message": "success",
                "model_info": {"model_id": common_job_parameters.model_id, "model_version": common_job_parameters.model_version},
                "logs_directory": logs_directory,
                "board_url": job_utils.get_board_url(job_id, job_initiator["role"], job_initiator["party_id"])
            }
            warn_parameter = JobRuntimeConfigAdapter(submit_job_conf.runtime_conf).check_removed_parameter()
            if warn_parameter:
                result["message"] = f"[WARN]{warn_parameter} is removed,it does not take effect!"
            submit_result.update(result)
            submit_result.update(path_dict)
        except Exception as e:
            submit_result["code"] = RetCode.OPERATING_ERROR
            submit_result["message"] = exception_to_trace_string(e)
            schedule_logger(job_id).exception(e)
        return submit_result

    @classmethod
    def update_parameters(cls, job, job_parameters, component_parameters):
        updated_job_parameters, updated_component_parameters, updated_components = JobController.gen_updated_parameters(job_id=job.f_job_id,
                                                                                                                        initiator_role=job.f_initiator_role,
                                                                                                                        initiator_party_id=job.f_initiator_party_id,
                                                                                                                        input_job_parameters=job_parameters,
                                                                                                                        input_component_parameters=component_parameters)
        schedule_logger(job.f_job_id).info(f"components {updated_components} parameters has been updated")
        updated_parameters = {
            "job_parameters": updated_job_parameters,
            "component_parameters": updated_component_parameters,
            "components": updated_components
        }
        status_code, response = FederatedScheduler.update_parameter(job, updated_parameters=updated_parameters)
        if status_code == FederatedSchedulingStatusCode.SUCCESS:
            return RetCode.SUCCESS, updated_parameters
        else:
            return RetCode.OPERATING_ERROR, response

    def run_do(self):
        schedule_logger().info("start schedule waiting jobs")
        jobs = JobSaver.query_job(is_initiator=True, status=JobStatus.WAITING, order_by="create_time", reverse=False)
        schedule_logger().info(f"have {len(jobs)} waiting jobs")
        if len(jobs):
            # FIFO
            job = jobs[0]
            schedule_logger().info(f"schedule waiting job {job.f_job_id}")
            try:
                self.schedule_waiting_jobs(job=job, lock=True)
            except Exception as e:
                schedule_logger(job.f_job_id).exception(e)
                schedule_logger(job.f_job_id).error("schedule waiting job failed")
        schedule_logger().info("schedule waiting jobs finished")

        schedule_logger().info("start schedule running jobs")
        jobs = JobSaver.query_job(is_initiator=True, status=JobStatus.RUNNING, order_by="create_time", reverse=False)
        schedule_logger().info(f"have {len(jobs)} running jobs")
        for job in jobs:
            schedule_logger().info(f"schedule running job {job.f_job_id}")
            try:
                self.schedule_running_job(job=job, lock=True)
            except Exception as e:
                schedule_logger(job.f_job_id).exception(e)
                schedule_logger(job.f_job_id).error("schedule job failed")
        schedule_logger().info("schedule running jobs finished")

        # some ready job exit before start
        schedule_logger().info("start schedule ready jobs")
        jobs = JobSaver.query_job(is_initiator=True, ready_signal=True, order_by="create_time", reverse=False)
        schedule_logger().info(f"have {len(jobs)} ready jobs")
        for job in jobs:
            schedule_logger().info(f"schedule ready job {job.f_job_id}")
            try:
                self.schedule_ready_job(job=job)
            except Exception as e:
                schedule_logger(job.f_job_id).exception(e)
                schedule_logger(job.f_job_id).error(f"schedule ready job failed:\n{e}")
        schedule_logger().info("schedule ready jobs finished")

        schedule_logger().info("start schedule rerun jobs")
        jobs = JobSaver.query_job(is_initiator=True, rerun_signal=True, order_by="create_time", reverse=False)
        schedule_logger().info(f"have {len(jobs)} rerun jobs")
        for job in jobs:
            schedule_logger(job.f_job_id).info(f"schedule rerun job {job.f_job_id}")
            try:
                self.schedule_rerun_job(job=job, lock=True)
            except Exception as e:
                schedule_logger(job.f_job_id).exception(e)
                schedule_logger(job.f_job_id).error("schedule job failed")
        schedule_logger().info("schedule rerun jobs finished")

        schedule_logger().info("start schedule end status jobs to update status")
        jobs = JobSaver.query_job(is_initiator=True, status=set(EndStatus.status_list()), end_time=[current_timestamp() - JobDefaultConfig.end_status_job_scheduling_time_limit, current_timestamp()])
        schedule_logger().info(f"have {len(jobs)} end status jobs")
        for job in jobs:
            schedule_logger().info(f"schedule end status job {job.f_job_id}")
            try:
                update_status = self.end_scheduling_updates(job_id=job.f_job_id)
                if update_status:
                    schedule_logger(job.f_job_id).info("try update status by scheduling like running job")
                else:
                    schedule_logger(job.f_job_id).info("the number of updates has been exceeded")
                    continue
                self.schedule_running_job(job=job, force_sync_status=True, lock=True)
            except Exception as e:
                schedule_logger(job.f_job_id).exception(e)
                schedule_logger(job.f_job_id).error("schedule job failed")
        schedule_logger().info("schedule end status jobs finished")

    @classmethod
    @schedule_utils.schedule_lock
    def schedule_waiting_jobs(cls, job):
        job_id, initiator_role, initiator_party_id, = job.f_job_id, job.f_initiator_role, job.f_initiator_party_id,
        if job.f_cancel_signal:
            job.f_status = JobStatus.CANCELED
            FederatedScheduler.sync_job_status(job=job)
            schedule_logger(job_id).info("job have cancel signal")
            return
        if job.f_inheritance_status != JobInheritanceStatus.PASS:
            cls.check_component(job)
        schedule_logger(job_id).info("job dependence check")
        dependence_status_code, federated_dependence_response = FederatedScheduler.dependence_for_job(job=job)
        schedule_logger(job_id).info(f"dependence check: {dependence_status_code}, {federated_dependence_response}")
        if dependence_status_code == FederatedSchedulingStatusCode.SUCCESS:
            apply_status_code, federated_response = FederatedScheduler.resource_for_job(job=job, operation_type=ResourceOperation.APPLY)
            if apply_status_code == FederatedSchedulingStatusCode.SUCCESS:
                cls.start_job(job_id=job_id, initiator_role=initiator_role, initiator_party_id=initiator_party_id)
            else:
                # rollback resource
                rollback_party = {}
                failed_party = {}
                for dest_role in federated_response.keys():
                    for dest_party_id in federated_response[dest_role].keys():
                        retcode = federated_response[dest_role][dest_party_id]["retcode"]
                        if retcode == 0:
                            rollback_party[dest_role] = rollback_party.get(dest_role, [])
                            rollback_party[dest_role].append(dest_party_id)
                        else:
                            failed_party[dest_role] = failed_party.get(dest_role, [])
                            failed_party[dest_role].append(dest_party_id)
                schedule_logger(job_id).info("job apply resource failed on {}, rollback {}".format(
                    ",".join([",".join([f"{_r}:{_p}" for _p in _ps]) for _r, _ps in failed_party.items()]),
                    ",".join([",".join([f"{_r}:{_p}" for _p in _ps]) for _r, _ps in rollback_party.items()]),
                ))
                if rollback_party:
                    return_status_code, federated_response = FederatedScheduler.resource_for_job(job=job, operation_type=ResourceOperation.RETURN, specific_dest=rollback_party)
                    if return_status_code != FederatedSchedulingStatusCode.SUCCESS:
                        schedule_logger(job_id).info(f"job return resource failed:\n{federated_response}")
                else:
                    schedule_logger(job_id).info("job no party should be rollback resource")
                if apply_status_code == FederatedSchedulingStatusCode.ERROR:
                    cls.stop_job(job_id=job_id, role=initiator_role, party_id=initiator_party_id, stop_status=JobStatus.FAILED)
                    schedule_logger(job_id).info("apply resource error, stop job")
        else:
            retcode_set = set()
            for dest_role in federated_dependence_response.keys():
                for party_id in federated_dependence_response[dest_role].keys():
                    retcode_set.add(federated_dependence_response[dest_role][party_id]["retcode"])
            if not retcode_set.issubset({RetCode.RUNNING, RetCode.SUCCESS}):
                FederatedScheduler.stop_job(job, StatusSet.FAILED)

    @classmethod
    def check_component(cls, job, check_type="inheritance"):
        schedule_logger(job.f_job_id).info("component check")
        dependence_status_code, response = FederatedScheduler.check_component(job=job, check_type=check_type)
        schedule_logger(job.f_job_id).info(f"component check response: {response}")
        dsl_parser = schedule_utils.get_job_dsl_parser(dsl=job.f_dsl,
                                                       runtime_conf=job.f_runtime_conf,
                                                       train_runtime_conf=job.f_train_runtime_conf)
        component_set = set([cpn.name for cpn in dsl_parser.get_source_connect_sub_graph(job.f_inheritance_info.get("component_list"))])
        for dest_role in response.keys():
            for party_id in response[dest_role].keys():
                component_set = component_set.intersection(set(response[dest_role][party_id].get("data")))
        if component_set != set(job.f_inheritance_info.get("component_list")):
            schedule_logger(job.f_job_id).info(f"dsl parser components:{component_set}")

            component_list = [cpn.name for cpn in dsl_parser.get_source_connect_sub_graph(list(component_set))]
            schedule_logger(job.f_job_id).info(f"parser result:{component_list}")
            command_body = {"inheritance_info": job.f_inheritance_info}
            command_body["inheritance_info"].update({"component_list": component_list})
            schedule_logger(job.f_job_id).info(f"start align job info:{command_body}")
            status_code, response = FederatedScheduler.align_args(job, command_body=command_body)
            schedule_logger(job.f_job_id).info(f"align result:{status_code}, {response}")
        schedule_logger(job.f_job_id).info("check success")

    @classmethod
    def schedule_ready_job(cls, job):
        job_id = job.f_job_id
        update_status = schedule_utils.ready_signal(job_id=job_id, set_or_reset=False, ready_timeout_ttl=60 * 1000)
        schedule_logger(job_id).info(f"reset job ready signal {update_status}")

    @classmethod
    @schedule_utils.schedule_lock
    def schedule_rerun_job(cls, job):
        if EndStatus.contains(job.f_status):
            job.f_status = JobStatus.WAITING
            job.f_ready_signal = False
            job.f_ready_time = None
            job.f_rerun_signal = False
            job.f_progress = 0
            job.f_end_time = None
            job.f_elapsed = None
            schedule_logger(job.f_job_id).info("job has been finished, set waiting to rerun")
            status, response = FederatedScheduler.sync_job_status(job=job)
            if status == FederatedSchedulingStatusCode.SUCCESS:
                schedule_utils.rerun_signal(job_id=job.f_job_id, set_or_reset=False)
                FederatedScheduler.sync_job(job=job, update_fields=["ready_signal", "ready_time", "rerun_signal", "progress", "end_time", "elapsed"])
                schedule_logger(job.f_job_id).info("job set waiting to rerun successfully")
            else:
                schedule_logger(job.f_job_id).info("job set waiting to rerun failed")
        else:
            schedule_utils.rerun_signal(job_id=job.f_job_id, set_or_reset=False)
            cls.schedule_running_job(job)

    @classmethod
    def start_job(cls, job_id, initiator_role, initiator_party_id):
        schedule_logger(job_id).info(f"try to start job on initiator {initiator_role} {initiator_party_id}")
        jobs = JobSaver.query_job(job_id=job_id, role=initiator_role, party_id=initiator_party_id)
        if jobs:
            job = jobs[0]
            FederatedScheduler.start_job(job=job)
            schedule_logger(job_id).info(f"start job on initiator {initiator_role} {initiator_party_id}")
        else:
            schedule_logger(job_id).error(f"can not found job on initiator {initiator_role} {initiator_party_id}")

    @classmethod
    @schedule_utils.schedule_lock
    def schedule_running_job(cls, job: Job, force_sync_status=False):
        schedule_logger(job.f_job_id).info("scheduling running job")

        dsl_parser = schedule_utils.get_job_dsl_parser(dsl=job.f_dsl,
                                                       runtime_conf=job.f_runtime_conf_on_party,
                                                       train_runtime_conf=job.f_train_runtime_conf)
        task_scheduling_status_code, auto_rerun_tasks, tasks = TaskScheduler.schedule(job=job, dsl_parser=dsl_parser, canceled=job.f_cancel_signal)
        tasks_status = dict([(task.f_component_name, task.f_status) for task in tasks])
        new_job_status = cls.calculate_job_status(task_scheduling_status_code=task_scheduling_status_code, tasks_status=tasks_status.values())
        if new_job_status == JobStatus.WAITING and job.f_cancel_signal:
            new_job_status = JobStatus.CANCELED
        total, finished_count = cls.calculate_job_progress(tasks_status=tasks_status)
        new_progress = float(finished_count) / total * 100
        schedule_logger(job.f_job_id).info(f"job status is {new_job_status}, calculate by task status list: {tasks_status}")
        if new_job_status != job.f_status or new_progress != job.f_progress:
            # Make sure to update separately, because these two fields update with anti-weight logic
            if int(new_progress) - job.f_progress > 0:
                job.f_progress = new_progress
                FederatedScheduler.sync_job(job=job, update_fields=["progress"])
                cls.update_job_on_initiator(initiator_job=job, update_fields=["progress"])
            if new_job_status != job.f_status:
                job.f_status = new_job_status
                if EndStatus.contains(job.f_status):
                    FederatedScheduler.save_pipelined_model(job=job)
                FederatedScheduler.sync_job_status(job=job)
                cls.update_job_on_initiator(initiator_job=job, update_fields=["status"])
        if EndStatus.contains(job.f_status):
            cls.finish(job=job, end_status=job.f_status)
        if auto_rerun_tasks:
            schedule_logger(job.f_job_id).info("job have auto rerun tasks")
            cls.set_job_rerun(job_id=job.f_job_id, initiator_role=job.f_initiator_role, initiator_party_id=job.f_initiator_party_id, tasks=auto_rerun_tasks, auto=True)
        if force_sync_status:
            FederatedScheduler.sync_job_status(job=job)
        schedule_logger(job.f_job_id).info("finish scheduling running job")

    @classmethod
    def set_job_rerun(cls, job_id, initiator_role, initiator_party_id, auto, force=False,
                      tasks: typing.List[Task] = None, component_name: typing.Union[str, list] = None):
        schedule_logger(job_id).info(f"try to rerun job on initiator {initiator_role} {initiator_party_id}")

        jobs = JobSaver.query_job(job_id=job_id, role=initiator_role, party_id=initiator_party_id)
        if not jobs:
            raise RuntimeError(f"can not found job on initiator {initiator_role} {initiator_party_id}")
        job = jobs[0]

        dsl_parser = schedule_utils.get_job_dsl_parser(dsl=job.f_dsl,
                                                       runtime_conf=job.f_runtime_conf_on_party,
                                                       train_runtime_conf=job.f_train_runtime_conf)
        component_name, force = cls.get_rerun_component(component_name, job, dsl_parser, force)
        schedule_logger(job_id).info(f"rerun component: {component_name}")

        if tasks:
            schedule_logger(job_id).info(f"require {[task.f_component_name for task in tasks]} to rerun")
        else:
            task_query = {
                'job_id': job_id,
                'role': initiator_role,
                'party_id': initiator_party_id,
            }

            if not component_name or component_name == job_utils.PIPELINE_COMPONENT_NAME:
                # rerun all tasks
                schedule_logger(job_id).info("require all component of pipeline to rerun")
            else:
                _require_reruns = {component_name} if isinstance(component_name, str) else set(component_name)
                _should_reruns = _require_reruns.copy()
                for _cpn in _require_reruns:
                    _components = dsl_parser.get_downstream_dependent_components(_cpn)
                    for _c in _components:
                        _should_reruns.add(_c.get_name())

                schedule_logger(job_id).info(f"require {_require_reruns} to rerun, "
                                             f"and then found {_should_reruns} need be to rerun")
                task_query['component_name'] = _should_reruns

            tasks = JobSaver.query_task(**task_query)

        job_can_rerun = any([TaskScheduler.prepare_rerun_task(
            job=job, task=task, dsl_parser=dsl_parser, auto=auto, force=force,
        ) for task in tasks])
        if not job_can_rerun:
            FederatedScheduler.sync_job_status(job=job)
            schedule_logger(job_id).info("job no task to rerun")
            return False

        schedule_logger(job_id).info("job set rerun signal")
        status = schedule_utils.rerun_signal(job_id=job_id, set_or_reset=True)
        schedule_logger(job_id).info(f"job set rerun signal {'successfully' if status else 'failed'}")
        return True

    @classmethod
    def get_rerun_component(cls, component_name, job, dsl_parser, force):
        if not component_name or component_name == job_utils.PIPELINE_COMPONENT_NAME:
            pass
        else:
            dependence_status_code, response = FederatedScheduler.check_component(job=job, check_type="rerun")
            success_task_list = [task.f_component_name for task in JobSaver.query_task(job_id=job.f_job_id, party_id=job.f_party_id, role=job.f_role,
                                                                                       status=TaskStatus.SUCCESS, only_latest=True)]
            component_set = set()
            for dest_role in response.keys():
                for party_id in response[dest_role].keys():
                    component_set = component_set.union(set(response[dest_role][party_id].get("data")))
            schedule_logger(job.f_job_id).info(f"success task list: {success_task_list}, check failed component list: {list(component_set)}")
            need_rerun = [cpn.name for cpn in dsl_parser.get_need_revisit_nodes(success_task_list, list(component_set))]
            schedule_logger(job.f_job_id).info(f"need rerun success component: {need_rerun}")
            if component_set:
                force = True
            if isinstance(component_name, str):
                component_name = set(need_rerun).union({component_name})
            else:
                component_name = set(need_rerun).union(set(component_name))
        return component_name, force

    @classmethod
    def update_job_on_initiator(cls, initiator_job: Job, update_fields: list):
        schedule_logger(initiator_job.f_job_id).info(f"try to update job {update_fields} on initiator")
        jobs = JobSaver.query_job(job_id=initiator_job.f_job_id)
        if not jobs:
            raise Exception("Failed to update job status on initiator")
        job_info = initiator_job.to_human_model_dict(only_primary_with=update_fields)
        for field in update_fields:
            job_info[field] = getattr(initiator_job, "f_%s" % field)
        for job in jobs:
            job_info["role"] = job.f_role
            job_info["party_id"] = job.f_party_id
            JobSaver.update_job_status(job_info=job_info)
            JobSaver.update_job(job_info=job_info)
        schedule_logger(initiator_job.f_job_id).info(f"update job {update_fields} on initiator finished")

    @classmethod
    def calculate_job_status(cls, task_scheduling_status_code, tasks_status):
        # 1. all waiting
        # 2. have running
        # 3. waiting + end status
        # 4. all end status and difference
        # 5. all the same end status
        tmp_status_set = set(tasks_status)
        if TaskStatus.PASS in tmp_status_set:
            tmp_status_set.remove(TaskStatus.PASS)
            tmp_status_set.add(TaskStatus.SUCCESS)
        if len(tmp_status_set) == 1:
            # 1 and 5
            return tmp_status_set.pop()
        else:
            if TaskStatus.RUNNING in tmp_status_set:
                # 2
                return JobStatus.RUNNING
            if TaskStatus.WAITING in tmp_status_set:
                # 3
                if task_scheduling_status_code == SchedulingStatusCode.HAVE_NEXT:
                    return JobStatus.RUNNING
                else:
                    # have waiting with no next
                    pass
            # have waiting with no next or 4
            for status in sorted(InterruptStatus.status_list(), key=lambda s: StatusSet.get_level(status=s), reverse=True):
                if status in tmp_status_set:
                    return status
            if tmp_status_set == {TaskStatus.WAITING, TaskStatus.SUCCESS} and task_scheduling_status_code == SchedulingStatusCode.NO_NEXT:
                return JobStatus.CANCELED

            raise Exception("calculate job status failed, all task status: {}".format(tasks_status))

    @classmethod
    def calculate_job_progress(cls, tasks_status):
        total = 0
        finished_count = 0
        for task_status in tasks_status.values():
            total += 1
            if EndStatus.contains(task_status):
                finished_count += 1
        return total, finished_count

    @classmethod
    def stop_job(cls, job_id, role, party_id, stop_status):
        schedule_logger(job_id).info(f"request stop job with {stop_status}")
        jobs = JobSaver.query_job(job_id=job_id, role=role, party_id=party_id, is_initiator=True)
        if len(jobs) > 0:
            if stop_status == JobStatus.CANCELED:
                schedule_logger(job_id).info("cancel job")
                set_cancel_status = schedule_utils.cancel_signal(job_id=job_id, set_or_reset=True)
                schedule_logger(job_id).info(f"set job cancel signal {set_cancel_status}")
            job = jobs[0]
            job.f_status = stop_status
            schedule_logger(job_id).info(f"request stop job with {stop_status} to all party")
            status_code, response = FederatedScheduler.stop_job(job=jobs[0], stop_status=stop_status)
            if status_code == FederatedSchedulingStatusCode.SUCCESS:
                schedule_logger(job_id).info(f"stop job with {stop_status} successfully")
                return RetCode.SUCCESS, "success"
            else:
                initiator_tasks_group = JobSaver.get_tasks_asc(job_id=job.f_job_id, role=job.f_role, party_id=job.f_party_id)
                for initiator_task in initiator_tasks_group.values():
                    TaskScheduler.collect_task_of_all_party(job, initiator_task=initiator_task, set_status=stop_status)
                schedule_logger(job_id).info(f"stop job with {stop_status} failed, {response}")
                return RetCode.FEDERATED_ERROR, json_dumps(response)
        else:
            return RetCode.SUCCESS, "can not found job"


    @classmethod
    @DB.connection_context()
    def end_scheduling_updates(cls, job_id):
        operate = Job.update({Job.f_end_scheduling_updates: Job.f_end_scheduling_updates + 1}).where(Job.f_job_id == job_id,
                                                                                                     Job.f_end_scheduling_updates < JobDefaultConfig.end_status_job_scheduling_updates)
        update_status = operate.execute() > 0
        return update_status

    @classmethod
    def finish(cls, job, end_status):
        schedule_logger(job.f_job_id).info(f"job finished with {end_status}, do something...")
        cls.stop_job(job_id=job.f_job_id, role=job.f_initiator_role, party_id=job.f_initiator_party_id, stop_status=end_status)
        FederatedScheduler.clean_job(job=job)
        schedule_logger(job.f_job_id).info(f"job finished with {end_status}, done")
