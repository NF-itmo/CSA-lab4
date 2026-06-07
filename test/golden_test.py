import pytest
import logging
import yaml

from src.Translator import Translator
from src.Viewer import Viewer
from src.Machine import Simulation, MachineConfig


def remove_cr(string: str) -> str:
    return string.replace("\n", "")


@pytest.mark.golden_test("golden/*.yaml")
def test_translator_and_machine(golden: dict[str, str], caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    programm_bytes, data_bytes = Translator(
        golden["in_src"]
    )()

    print(programm_bytes.hex(' '))
    print(data_bytes.hex(' '))

    assert programm_bytes.hex(' ') == remove_cr(golden["out_code_binary_hex"])
    assert data_bytes.hex(' ') == remove_cr(golden["out_data_binary_hex"])

    viewer_out = Viewer(programm_bytes, data_bytes)()
    print(viewer_out)
    
    assert viewer_out == golden["out_code_view"]

    config_dict = yaml.safe_load(golden["in_simulation_conf"])
    machine_config = MachineConfig.from_dict(config_dict)

    sim_obj = Simulation(
        data_bytes,
        programm_bytes,
        machine_config.limit,
        machine_config.port_mapped_io,
        machine_config.log_configs
    )
    try:
        sim_obj.start()
    except Exception:
        pass
    finally:
        print(sim_obj.logs)
    
    assert sim_obj.logs[1:-1] == golden["out_machine"]