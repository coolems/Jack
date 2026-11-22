"""Keep an already-reset Windows socket from producing a second teardown error."""

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def is_proactor_shutdown_reset(context):
    error = context.get("exception")
    if (not isinstance(error, ConnectionResetError) or getattr(error, "winerror", None) != 10054
            or not context.get("message", "").startswith(
                "Exception in callback _ProactorBasePipeTransport._call_connection_lost(")):
        return False
    tb = error.__traceback__
    if tb is None:
        return False
    while tb.tb_next is not None:
        tb = tb.tb_next
    frame = tb.tb_frame
    return (frame.f_globals.get("__name__") == "asyncio.proactor_events"
            and frame.f_code.co_name == "_call_connection_lost")


def install_teardown_handler():
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop, context):
        if is_proactor_shutdown_reset(context):
            logger.debug("Peer reset a Windows socket before asyncio finished closing it")
        elif previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
