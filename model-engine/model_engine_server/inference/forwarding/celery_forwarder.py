import argparse
import json
from typing import Any, Dict, Optional, TypedDict, Union

from celery import Celery, Task, states
from model_engine_server.common.constants import DEFAULT_CELERY_TASK_NAME, LIRA_CELERY_TASK_NAME
from model_engine_server.common.dtos.model_endpoints import BrokerType
from model_engine_server.core.celery import TaskVisibility, celery_app
from model_engine_server.core.config import infra_config
from model_engine_server.core.loggers import logger_name, make_logger
from model_engine_server.core.utils.format import format_stacktrace
from model_engine_server.inference.forwarding.forwarding import (
    Forwarder,
    LoadForwarder,
    load_named_config,
)

logger = make_logger(logger_name())


class ErrorResponse(TypedDict):
    """The response payload for any inference request that encountered an error."""

    error: str
    error_metadata: str


class ErrorHandlingTask(Task):
    """Sets a 'custom' field with error in the Task response for FAILURE.

    Used when services are ran via the Celery backend.
    """

    def after_return(
        self, status: str, retval: Union[dict, Exception], task_id: str, args, kwargs, einfo
    ) -> None:
        """Handler that ensures custom error response information is available whenever a Task fails.

        Specifically, whenever the task's :param:`status` is `"FAILURE"` and the return value
        :param:`retval` is an `Exception`, this handler extracts information from the `Exception`
        and constructs a custom error response JSON value (see :func:`error_response` for details).

        This handler then re-propagates the Celery-required exception information (`"exc_type"` and
        `"exc_message"`) while adding this new error response information under the `"custom"` key.
        """
        if status == states.FAILURE and isinstance(retval, Exception):
            logger.warning(f"Setting custom error response for failed task {task_id}")

            info: dict = raw_celery_response(self.backend, task_id)
            result: dict = info["result"]
            err: Exception = retval

            error_payload = error_response("Internal failure", err)

            # Inspired by pattern from:
            # https://www.distributedpython.com/2018/09/28/celery-task-states/
            self.update_state(
                state=states.FAILURE,
                meta={
                    "exc_type": result["exc_type"],
                    "exc_message": result["exc_message"],
                    "custom": json.dumps(error_payload, indent=False),
                },
            )


def raw_celery_response(backend, task_id: str) -> Dict[str, Any]:
    key_info: str = backend.get_key_for_task(task_id)
    info_as_str: str = backend.get(key_info)
    info: dict = json.loads(info_as_str)
    return info


def error_response(msg: str, e_unhandled: Exception) -> ErrorResponse:
    stacktrace = format_stacktrace(e_unhandled)
    return {
        "error": str(e_unhandled),
        "error_metadata": f"{msg}\n{stacktrace}",
    }


def create_celery_service(
    forwarder: Forwarder,
    task_visibility: TaskVisibility,
    queue_name: Optional[str] = None,
    sqs_url: Optional[str] = None,
) -> Celery:
    """
    Creates a celery application.
    Returns:
        app (celery.app.base.Celery): Celery app.
        exec_func (celery.local.PromiseProxy): Callable task function.
    """

    app: Celery = celery_app(
        name=None,
        s3_bucket=infra_config().s3_bucket,
        task_visibility=task_visibility,
        broker_type=str(BrokerType.SQS.value if sqs_url else BrokerType.REDIS.value),
        broker_transport_options={"predefined_queues": {queue_name: {"url": sqs_url}}}
        if sqs_url
        else None,
    )

    # See documentation for options:
    # https://docs.celeryproject.org/en/stable/userguide/tasks.html#list-of-options
    @app.task(base=ErrorHandlingTask, name=LIRA_CELERY_TASK_NAME, track_started=True)
    def exec_func(payload, *ignored_args, **ignored_kwargs):
        if len(ignored_args) > 0:
            logger.warning(f"Ignoring {len(ignored_args)} positional arguments: {ignored_args=}")
        if len(ignored_kwargs) > 0:
            logger.warning(f"Ignoring {len(ignored_kwargs)} keyword arguments: {ignored_kwargs=}")
        try:
            return forwarder(payload)
        except Exception:
            logger.exception("Celery service failed to respond to request.")
            raise

    # Have celery service also accept pre-LIRA celery task name to ensure no downtime
    # when transitioning from pre-LIRA single container architecture to LIRA
    # multi-container-architecture.
    @app.task(
        base=ErrorHandlingTask,
        name=DEFAULT_CELERY_TASK_NAME,
        track_started=True,
    )
    def exec_func_pre_lira(payload, *ignored_args, **ignored_kwargs):
        return exec_func(payload, *ignored_args, **ignored_kwargs)

    return app


def start_celery_service(
    app: Celery,
    queue: str,
    concurrency: int,
) -> None:
    worker = app.Worker(
        queues=[queue],
        concurrency=concurrency,
        loglevel="INFO",
        optimization="fair",
        # pool="solo" argument fixes the known issues of celery and some of the libraries.
        # Particularly asyncio and torchvision transformers.
        pool="solo",
    )
    worker.start()


def entrypoint():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--set", type=str, action="append")
    parser.add_argument("--task-visibility", type=str, required=True)
    parser.add_argument("--num-workers", type=int, required=True)
    parser.add_argument("--queue", type=str, required=True)
    parser.add_argument("--sqs-url", type=str, default=None)

    args = parser.parse_args()

    forwarder_config = load_named_config(args.config, args.set)
    forwarder_loader = LoadForwarder(**forwarder_config["async"])
    forwader = forwarder_loader.load(None, None)

    app = create_celery_service(forwader, TaskVisibility.VISIBILITY_24H, args.queue, args.sqs_url)
    start_celery_service(app, args.queue, args.num_workers)


if __name__ == "__main__":
    entrypoint()
