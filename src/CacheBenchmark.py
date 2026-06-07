import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.Config import General, MachineConfig
from src.Machine import GLOBAL_NETLIST, Simulation
from src.Translator import Translator


@dataclass(frozen=True)
class CacheVariant:
    name: str
    line_size: int
    line_count: int
    way_count: int

    @property
    def capacity(self) -> int:
        return self.line_size * self.line_count


FIXED_CAPACITY_VARIANTS = [
    CacheVariant("4way_line32_cap256", line_size=32, line_count=8, way_count=4),
    CacheVariant("2way_line16_cap256", line_size=16, line_count=16, way_count=2),
    CacheVariant("4way_line16_cap256", line_size=16, line_count=16, way_count=4),
    CacheVariant("2way_line32_cap256", line_size=32, line_count=8, way_count=2),
]


def patch_cache_geometry(variant: CacheVariant) -> None:
    if variant.line_count % variant.way_count != 0:
        raise ValueError(f"{variant.name}: line_count must be divisible by way_count")

    General.CACHE_LINE_SIZE_BYTES = variant.line_size
    General.CACHE_LINE_COUNT = variant.line_count
    General.CACHE_WAY_COUNT = variant.way_count


def load_golden(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cache_stats(cache: Any) -> dict[str, int | float]:
    accesses = cache.access_count
    hits = cache.hit_count
    misses = cache.miss_count
    hit_rate = 0.0 if accesses == 0 else hits / accesses
    return {
        "accesses": accesses,
        "hits": hits,
        "misses": misses,
        "hit_rate": hit_rate,
    }


def combined_stats(*caches: ...) -> dict[str, int | float]:
    accesses = sum(cache.access_count for cache in caches)
    hits = sum(cache.hit_count for cache in caches)
    misses = accesses - hits
    hit_rate = 0.0 if accesses == 0 else hits / accesses
    return {
        "accesses": accesses,
        "hits": hits,
        "misses": misses,
        "hit_rate": hit_rate,
    }


def run_case(golden_path: Path, variant: CacheVariant) -> dict[str, Any]:
    patch_cache_geometry(variant)
    golden = load_golden(golden_path)
    code, data = Translator(golden["in_src"])()
    simulation_config = MachineConfig.from_dict(yaml.safe_load(golden["in_simulation_conf"]))

    sim = Simulation(
        data,
        code,
        simulation_config.limit,
        simulation_config.port_mapped_io,
        log_configs=[],
        cache_enabled=True,
    )

    result: dict[str, Any] = {
        "test": golden_path.stem,
        "variant": variant.name,
        "line_size": variant.line_size,
        "line_count": variant.line_count,
        "way_count": variant.way_count,
        "capacity": variant.capacity,
    }

    try:
        sim.start()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        machine = sim.machine
        result["ticks"] = GLOBAL_NETLIST.tick_counter
        result["code"] = cache_stats(machine.code_mem)
        result["data"] = cache_stats(machine.data_mem)
        result["total"] = combined_stats(machine.code_mem, machine.data_mem)

    return result


def aggregate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_variant: dict[str, dict[str, Any]] = {}
    for row in results:
        key = row["variant"]
        acc = by_variant.setdefault(
            key,
            {
                "test": "TOTAL",
                "variant": key,
                "line_size": row["line_size"],
                "line_count": row["line_count"],
                "way_count": row["way_count"],
                "capacity": row["capacity"],
                "ticks": 0,
                "code": {"accesses": 0, "hits": 0, "misses": 0},
                "data": {"accesses": 0, "hits": 0, "misses": 0},
                "total": {"accesses": 0, "hits": 0, "misses": 0},
                "errors": 0,
            },
        )
        acc["ticks"] += row.get("ticks", 0)
        if "error" in row:
            acc["errors"] += 1
        for scope in ("code", "data", "total"):
            for metric in ("accesses", "hits", "misses"):
                acc[scope][metric] += row[scope][metric]

    for acc in by_variant.values():
        for scope in ("code", "data", "total"):
            accesses = acc[scope]["accesses"]
            hits = acc[scope]["hits"]
            acc[scope]["hit_rate"] = 0.0 if accesses == 0 else hits / accesses

    return list(by_variant.values())


def flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    flat = {
        "test": row["test"],
        "variant": row["variant"],
        "line_size": row["line_size"],
        "line_count": row["line_count"],
        "way_count": row["way_count"],
        "capacity": row["capacity"],
        "ticks": row["ticks"],
    }
    for scope in ("code", "data", "total"):
        flat[f"{scope}_hit_rate"] = row[scope]["hit_rate"]
        flat[f"{scope}_accesses"] = row[scope]["accesses"]
        flat[f"{scope}_hits"] = row[scope]["hits"]
        flat[f"{scope}_misses"] = row[scope]["misses"]
    if "error" in row:
        flat["error"] = row["error"]
    if "errors" in row:
        flat["errors"] = row["errors"]
    return flat


def print_markdown(rows: list[dict[str, Any]]) -> None:
    headers = [
        "test",
        "variant",
        "line",
        "lines",
        "ways",
        "cap",
        "ticks",
        "total HR",
        "total acc",
        "total hits",
        "total miss",
        "code HR",
        "data HR",
        "errors",
    ]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        errors = row.get("errors", 1 if "error" in row else 0)
        values: list[str] = [
            row["test"],
            row["variant"],
            str(row["line_size"]),
            str(row["line_count"]),
            str(row["way_count"]),
            str(row["capacity"]),
            str(row["ticks"]),
            f"{row['total']['hit_rate']:.2%}",
            str(row["total"]["accesses"]),
            str(row["total"]["hits"]),
            str(row["total"]["misses"]),
            f"{row['code']['hit_rate']:.2%}",
            f"{row['data']['hit_rate']:.2%}",
            str(errors),
        ]
        print("| " + " | ".join(values) + " |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark cache geometry over golden tests")
    parser.add_argument("--golden-dir", type=Path, default=Path.cwd() / "test" / "golden")
    parser.add_argument("--per-test", action="store_true", help="Print per-test rows instead of totals only")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Run only selected golden file stem; can be passed multiple times",
    )
    args = parser.parse_args()

    variants = FIXED_CAPACITY_VARIANTS
    golden_paths = sorted(args.golden_dir.glob("*.yaml"))
    if args.only:
        selected = set(args.only)
        golden_paths = [path for path in golden_paths if path.stem in selected]
    if not golden_paths:
        raise RuntimeError(f"No golden files found in {args.golden_dir}")

    results = [
        run_case(path, variant)
        for variant in variants for path in golden_paths
    ]
    rows = results if args.per_test else aggregate(results)

    print_markdown(rows)
