from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

from xian_runtime_types.encoding import encode

from xian.legacy_genesis import (
    LegacyMigrationError,
    LegacyMigrationOptions,
    LegacyValidator,
    build_legacy_migration_genesis,
    load_legacy_export,
    verify_migration_genesis,
)
from xian.legacy_source import COMPAT_REWRITES


def _parse_validator(value: str) -> LegacyValidator:
    public_key, _, power = value.partition(":")
    return LegacyValidator(public_key=public_key.lower(), power=int(power) if power else 10)


def _parse_override(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise ValueError(f"expected NAME=PATH, got {value!r}")
    return name, Path(path)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description=(
            "Build a genesis.json for the current runtime from a legacy Xian "
            "exported_state.json (contracts, state and nonces)"
        )
    )
    parser.add_argument("--export", required=True, help="Legacy exported_state.json")
    parser.add_argument(
        "--export-sha256", help="Refuse the export unless its SHA-256 matches this value"
    )
    parser.add_argument("--chain-id", required=True)
    parser.add_argument(
        "--genesis-time",
        required=True,
        help="RFC 3339 UTC time, e.g. 2026-11-01T00:00:00Z. Pinned so every "
        "party rebuilding the genesis gets the same app hash.",
    )
    parser.add_argument(
        "--network", default="mainnet", help="Network contract bundle (contracts_<network>.json)"
    )
    parser.add_argument("--contracts-dir", help="Directory holding the contract bundles")
    parser.add_argument(
        "--runtime-features",
        default='{"zk": true}',
        help="JSON object of genesis runtime features",
    )
    parser.add_argument(
        "--initial-height",
        type=int,
        help="First block height of the new chain (default: legacy height + 1)",
    )
    parser.add_argument(
        "--validator",
        action="append",
        type=_parse_validator,
        help="Genesis validator as PUBKEY_HEX[:POWER]; repeatable. Defaults to the legacy "
        "masternodes.nodes list.",
    )
    parser.add_argument("--founder-key", help="Optional key that signs the genesis state")
    parser.add_argument(
        "--source-override",
        action="append",
        default=[],
        help="NAME=PATH: deploy this source instead of the recovered one; repeatable",
    )
    parser.add_argument(
        "--skip-contract",
        action="append",
        default=[],
        help="Drop a legacy contract and its state entirely; repeatable",
    )
    parser.add_argument(
        "--no-compat-rewrites",
        action="store_true",
        help=f"Disable compatibility rewrites ({', '.join(sorted(COMPAT_REWRITES))})",
    )
    parser.add_argument(
        "--no-collision-renames",
        action="store_true",
        help="Fail on contracts whose recovered names collide instead of renaming",
    )
    parser.add_argument(
        "--keep-masternode-holdings",
        action="store_true",
        help="Do not refund legacy masternode registration holdings to their depositors",
    )
    parser.add_argument("--output", required=True, help="Path for the generated genesis.json")
    parser.add_argument(
        "--report", help="Path for the migration report (default: <output>.report.json)"
    )
    parser.add_argument(
        "--sources-dir", help="Write every deployed contract source here for review"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = dict(_parse_override(value) for value in args.source_override)
    options = LegacyMigrationOptions(
        chain_id=args.chain_id,
        genesis_time=args.genesis_time,
        network=args.network,
        contracts_dir=Path(args.contracts_dir) if args.contracts_dir else None,
        runtime_features=json.loads(args.runtime_features),
        initial_height=args.initial_height,
        founder_private_key=args.founder_key,
        validators=args.validator,
        compat_rewrites=frozenset() if args.no_compat_rewrites else frozenset(COMPAT_REWRITES),
        resolve_name_collisions=not args.no_collision_renames,
        source_overrides={
            name: path.read_text(encoding="utf-8") for name, path in overrides.items()
        },
        skip_contracts=frozenset(args.skip_contract),
        refund_masternode_holdings=not args.keep_masternode_holdings,
    )

    try:
        export = load_legacy_export(Path(args.export), expected_sha256=args.export_sha256)
        result = build_legacy_migration_genesis(export, options)
        verification = verify_migration_genesis(result.genesis, export, result.report)
    except LegacyMigrationError as exc:
        print(f"legacy migration failed: {exc}", file=sys.stderr)
        return 1

    result.report["verification"] = verification
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encode(result.genesis), encoding="utf-8")
    report_path = Path(args.report) if args.report else output.with_suffix(".report.json")
    report_path.write_text(json.dumps(result.report, indent=2, sort_keys=True), encoding="utf-8")
    if args.sources_dir:
        sources_dir = Path(args.sources_dir)
        sources_dir.mkdir(parents=True, exist_ok=True)
        for name, source in sorted(result.recovered_sources.items()):
            (sources_dir / f"{name}.py").write_text(source, encoding="utf-8")

    summary = result.report["summary"]
    print(f"genesis:   {output}")
    print(f"report:    {report_path}")
    print(f"app_hash:  {verification['app_hash']}")
    print(f"contracts: {summary['user_contracts_migrated']} user contracts migrated")
    print(f"nonces:    {summary['nonces_carried']}")
    print(f"supply:    {summary['currency_supply']}")
    for warning in result.report["warnings"]:
        print(f"warning:   {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
