from datetime import datetime

from ..lib.service import Service, Holdoff
from .. import rpcutil
from web.model import PrinterState, PrinterStats, Heater

from libflagship.mqtt import MqttMsgType


def parse_leveling_grid(data):
    lines = data.splitlines()

    if not lines[0].startswith("Bilinear Leveling Grid:"):
        return

    if not lines[1].split()[0] == "0":
        return

    if not lines[2].split()[0] == "0":
        return

    res = []
    for line in lines[2:9]:
        res.append([float(n) for n in line.split()[1:]])

    return res


class UpdateNotifierService(Service):

    def mqtt_to_jsonrpc_req(self, data):
        update = {
            "eventtime": datetime.now().timestamp(),
        }

        match data.get("commandType", 0):
            case MqttMsgType.ZZ_MQTT_CMD_HOTBED_TEMP:
                self.pstate.hotbed = Heater.from_mqtt(data)
                update["heater_bed"] = {
                    "temperature": self.pstate.hotbed.current,
                    "target": self.pstate.hotbed.target,
                    "power": None,
                }

            case MqttMsgType.ZZ_MQTT_CMD_NOZZLE_TEMP:
                self.pstate.nozzle = Heater.from_mqtt(data)
                update["extruder"] = {
                    "temperature": self.pstate.nozzle.current,
                    "target": self.pstate.nozzle.target,
                    "power": 0,
                    "can_extrude": True,
                    "pressure_advance": None,
                    "smooth_time": None,
                    "motion_queue": None,
                }

            case MqttMsgType.ZZ_MQTT_CMD_AUTO_LEVELING:
                index = data.get("value", 0)
                if index < 50:
                    update["display_status"] = {
                        "message": "Bed leveling in progress..",
                        "progress": float(index) / 49.0,
                    }
                    update["virtual_sdcard"] = {
                        "progress": float(index) / 49.0,
                        "file_position": None
                    }
                    update["print_stats"] = {
                        "total_duration": 1,
                        "print_duration": 1,
                        "filament_used": 1,
                        "filename": None,
                        "state": "printing",
                    }
                else:
                    update["display_status"] = {
                        "message": None,
                        "progress": None,
                    }

            case MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE:
                total = 670
                time = data.get("time", 0)
                update["print_stats"] = {
                    "total_duration": total - time,
                    "print_duration": total - time,
                }

            case MqttMsgType.ZZ_MQTT_CMD_MOTOR_LOCK:
                locked = data.get("value", 0)
                update["toolhead"] = {
                    "homed_axes": "xyz" if locked else "",
                }

            case MqttMsgType.ZZ_MQTT_CMD_GCODE_COMMAND:
                result = data["resData"]
                if result.startswith("Bilinear Leveling Grid"):
                    mesh = parse_leveling_grid(result)
                    mesh_params = {
                        "min_x": 0,
                        "min_y": 0,
                        "max_x": 235,
                        "max_y": 235,
                        "x_count": 5,
                        "y_count": 5,
                        "mesh_x_pps": 5,
                        "mesh_y_pps": 5,
                        "algo": "bicubic",
                        "tension": 0.2
                    }
                    return rpcutil.make_jsonrpc_req("notify_status_update", {
                            "bed_mesh": {
                                "profile_name": "anker-builtin",
                                "mesh_min": [  0,   0],
                                "mesh_max": [235, 235],
                                "mesh_params": mesh_params,
                                "probed_matrix": mesh,
                                "profiles": {
                                    "anker-builtin": {
                                        "points": mesh,
                                        "mesh_params": mesh_params,
                                    }
                                }
                            }
                        },
                        datetime.now().timestamp()
                    )
                else:
                    return rpcutil.make_jsonrpc_req("notify_gcode_response", result)

            case _:
                return None

        return rpcutil.make_jsonrpc_req("notify_status_update", update)

    def notify_error(self, message):
        self.notify(rpcutil.make_jsonrpc_req("notify_gcode_response", f"!! {message}"))

    def notify_status_update(self, **kwargs):
        self.notify(rpcutil.make_jsonrpc_req("notify_status_update", kwargs, datetime.now().timestamp()))

    def notify_job_queue_changed(self, action, queue, state):
        self.notify(rpcutil.make_jsonrpc_req("notify_job_queue_changed", {
            "action": action,
            "updated_queue": queue,
            "queue_state": state,
        }))

    def _handler(self, data):
        upd = self.mqtt_to_jsonrpc_req(data)
        if upd:
            self.notify(upd)

    def worker_init(self):
        self.pstate = PrinterState(nozzle=Heater(), hotbed=Heater())
        self.pstats = PrinterStats(nozzle=[], hotbed=[])
        self.holdoff = Holdoff()
        self.holdoff.reset(delay=1)
        try:
            with open("stats.json") as fd:
                self.pstats.load(fd)
        except OSError:
            pass

    def worker_start(self):
        self.mqtt = self.app.svc.get("mqttqueue", ready=False)

        self.mqtt.handlers.append(self._handler)

    def worker_run(self, timeout):
        if self.holdoff.passed:
            self.holdoff.reset(delay=1)
            self.pstats.append(self.pstate)
            self.notify(rpcutil.make_jsonrpc_req("notify_status_update", {}))

        self.idle(timeout=timeout)

    def worker_stop(self):
        with open("stats.json", "w") as fd:
            self.pstats.save(fd)
        self.mqtt.handlers.remove(self._handler)

        self.app.svc.put("mqttqueue")
