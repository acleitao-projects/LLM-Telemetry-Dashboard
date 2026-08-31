from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import nautilus_agent
from observatory import database, metrics
from observatory.collector import Collector, _bucketize_gpu
from observatory.models import (BuildInfo, GpuTelemetrySample, HardwareInfo,
                                Model, Provider, SessionRow, TelemetrySample)
from observatory.session_tracker import ProviderState


def memory_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class BuildMetadataTests(unittest.TestCase):
    def test_migration_removes_empty_builds_and_preserves_props_build(self):
        engine = memory_engine()
        with Session(engine) as session:
            session.exec(text("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"))
            session.exec(text("INSERT INTO meta VALUES ('schema_version', '1')"))
            session.add(BuildInfo(provider_id=1, build_json='{"argv": []}'))
            session.add(BuildInfo(provider_id=1, version="b10666", commit="4e97ac86e"))
            session.commit()

        database._migrate(engine)

        with Session(engine) as session:
            builds = session.exec(select(BuildInfo)).all()
            version = session.exec(text(
                "SELECT value FROM meta WHERE key='schema_version'"
            )).first()[0]
            self.assertEqual(version, "7")
            self.assertEqual([(row.version, row.commit) for row in builds],
                             [("b10666", "4e97ac86e")])

    def test_props_is_stable_and_empty_agent_build_is_ignored(self):
        engine = memory_engine()
        collector = Collector(lambda _: None)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            props = {"build_info": "b10666-4e97ac86e"}
            collector._store_router_props(session, provider, props)
            collector._store_router_props(session, provider, props)
            empty_agent = {
                "info": {"hostname": "host"}, "gpu": {"gpus": []},
                "llama": {"version": None, "commit": None,
                           "docker_image": None, "container_id": None},
            }
            collector._store_agent(session, provider, empty_agent, 1000)
            collector._store_agent(session, provider, empty_agent, 2000)
            builds = session.exec(select(BuildInfo)).all()
            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0].version, "b10666")


class GpuCollectionTests(unittest.TestCase):
    def test_agent_emits_gpu_index_and_uuid(self):
        output = ("0, GPU-aaa, NVIDIA RTX 3060, 12288, 6000, 12, 52, 170, 40, 3, 16, 580.1\n"
                  "1, GPU-bbb, NVIDIA RTX 3060, 12288, 7000, 22, 48, 170, 35, 3, 16, 580.1\n")
        result = SimpleNamespace(returncode=0, stdout=output)
        with patch("nautilus_agent.shutil.which", return_value="nvidia-smi"), \
                patch("nautilus_agent.subprocess.run", return_value=result):
            payload = nautilus_agent.gpu_info()
        self.assertEqual([(gpu["index"], gpu["uuid"]) for gpu in payload["gpus"]],
                         [(0, "GPU-aaa"), (1, "GPU-bbb")])

    def test_two_gpu_samples_are_stored_once_with_active_context(self):
        engine = memory_engine()
        collector = Collector(lambda _: None)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            collector.model_states[(provider.id, "a")] = ProviderState(
                model_id=10, active_session_id=20, was_loaded=True,
            )
            collector.model_states[(provider.id, "b")] = ProviderState(
                model_id=11, active_session_id=21, was_loaded=True,
            )
            data = {
                "info": {"hostname": "host"}, "llama": {},
                "gpu": {"gpus": [
                    {"index": 0, "uuid": "GPU-aaa", "name": "RTX 3060",
                     "vram_mb": 12288, "vram_used_mb": 6000, "util": 10,
                     "temp_c": 50, "power_w": 40},
                    {"index": 1, "uuid": "GPU-bbb", "name": "RTX 3060",
                     "vram_mb": 12288, "vram_used_mb": 7000, "util": 20,
                     "temp_c": 45, "power_w": 30},
                ]},
            }
            collector._store_agent(session, provider, data, 1000)
            collector._store_agent(session, provider, data, 1000)
            rows = session.exec(select(GpuTelemetrySample).order_by(
                GpuTelemetrySample.gpu_index)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row.gpu_key for row in rows], ["GPU-aaa", "GPU-bbb"])
            self.assertEqual(json.loads(rows[0].active_model_ids), [10, 11])
            self.assertEqual(json.loads(rows[0].active_session_ids), [20, 21])

    def test_gpu_retention_averages_values_and_unions_active_context(self):
        engine = memory_engine()
        with Session(engine) as session:
            for ts, util, models in ((1100, 10, [1]), (1900, 30, [2])):
                session.add(GpuTelemetrySample(
                    provider_id=1, ts=ts, gpu_key="GPU-aaa", gpu_index=0,
                    gpu_uuid="GPU-aaa", util=util, active_model_ids=json.dumps(models),
                    active_session_ids=json.dumps(models),
                ))
            session.commit()
            _bucketize_gpu(session, 1000, 2000, 1000)
            rows = session.exec(select(GpuTelemetrySample)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].ts, 1000)
            self.assertEqual(rows[0].util, 20)
            self.assertEqual(json.loads(rows[0].active_model_ids), [1, 2])


class GpuApiTests(unittest.TestCase):
    def test_all_detail_apis_preserve_two_gpus_and_legacy_series(self):
        engine = memory_engine()
        now = int(time.time() * 1000)
        with Session(engine) as session:
            provider = Provider(name="router", base_url="http://router", status="LIVE")
            session.add(provider)
            session.commit()
            session.refresh(provider)
            model = Model(provider_id=provider.id, key="model", name="model")
            session.add(model)
            session.commit()
            session.refresh(model)
            run = SessionRow(provider_id=provider.id, model_id=model.id,
                             start_at=now - 30_000, end_at=now, status="CLOSED")
            session.add(run)
            session.commit()
            session.refresh(run)
            session.add(HardwareInfo(provider_id=provider.id, hostname="host", gpus=json.dumps([
                {"index": 0, "uuid": "GPU-aaa", "name": "RTX 3060", "vram_mb": 12288},
                {"index": 1, "uuid": "GPU-bbb", "name": "RTX 3060", "vram_mb": 12288},
            ])))
            session.add(BuildInfo(provider_id=provider.id, version="b10666", commit="4e97ac86e"))
            for offset, total in ((-20_000, 10), (-10_000, 20)):
                session.add(TelemetrySample(
                    provider_id=provider.id, model_id=model.id, session_id=run.id,
                    ts=now + offset, state="GENERATING", tokens_total=total,
                    gen_total=total, gpu_util=10, vram_used_mb=6000,
                ))
                for index, key, util, vram in (
                    (0, "GPU-aaa", 10, 6000), (1, "GPU-bbb", 20, 7000),
                ):
                    session.add(GpuTelemetrySample(
                        provider_id=provider.id, ts=now + offset, gpu_key=key,
                        gpu_index=index, gpu_uuid=key, name="RTX 3060", util=util,
                        vram_used_mb=vram, vram_total_mb=12288, temp_c=50 - index,
                        power_w=40 - index, active_model_ids=json.dumps([model.id]),
                        active_session_ids=json.dumps([run.id]),
                    ))
            session.commit()

            hardware = metrics.hardware(session)
            model_data = metrics.model_detail(session, model.id, "1h")
            session_data = metrics.session_detail(session, run.id)
            compare = metrics.compare(session, [run.id])

            self.assertEqual(len(hardware["providers"][0]["graphs"]["gpus"]), 2)
            self.assertEqual(hardware["providers"][0]["build"]["source"],
                             "llama.cpp /props")
            self.assertEqual(len(model_data["graphs"]["gpus"]), 2)
            self.assertIn("gpu_util", model_data["graphs"]["series"])
            self.assertEqual(len(session_data["graphs"]["gpus"]), 2)
            self.assertEqual(len(session_data["session"]["gpus"]), 2)
            self.assertIn("gpu_util", session_data["graphs"]["series"])
            self.assertEqual(len(compare["sessions"][0]["gpus"]), 2)


if __name__ == "__main__":
    unittest.main()
