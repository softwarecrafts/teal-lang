"""Run with multiple processes - sort of emulates AWS Lambda"""
import multiprocessing
import time
import traceback
import warnings

from ..controllers import ddb as ddb_controller
from ..controllers import ddb_model as db
from ..machine import TlMachine
from ..machine.probe import Probe


class Invoker:
    def __init__(self, data_controller):
        self.data_controller = data_controller
        self.exception = None

    def invoke(self, vmid, run_async=True):
        event = dict(
            # --
            session_id=self.data_controller.session_id,
            vmid=vmid,
        )
        p = multiprocessing.Process(target=resume_handler, args=(event,))
        p.start()


def resume_handler(event):
    # TODO catch exceptions and send them back!
    session_id = event["session_id"]
    vmid = event["vmid"]
    session = db.SessionItem.get(session_id, "meta")
    controller = ddb_controller.DataController(session)
    invoker = Invoker(controller)

    machine = TlMachine(vmid, invoker)
    machine.run()
    # TODO return something
