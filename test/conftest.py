import yaml
from pathlib import Path
import pytest


def pytest_generate_tests(metafunc: pytest.Metafunc):
    if "golden" in metafunc.fixturenames:
        marker = metafunc.definition.get_closest_marker("golden_test")
        if marker:
            pattern = marker.args[0]
            
            test_dir = Path(metafunc.definition.fspath).parent
            glob_pattern = pattern
            yaml_files = sorted(test_dir.glob(glob_pattern))
            
            test_data: list[dict[str, str]] = []
            test_ids: list[str] = []

            for yaml_file in yaml_files:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                test_data.append(data)
                test_ids.append(yaml_file.stem)
            
            metafunc.parametrize("golden", test_data, ids=test_ids)


def pytest_configure(config: pytest.Config):
    config.addinivalue_line(
        "markers", "golden_test(pattern): mark test as a golden test with YAML pattern"
    )
