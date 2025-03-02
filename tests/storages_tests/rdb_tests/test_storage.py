import os
import pickle
import shutil
import sys
import tempfile
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from optuna import create_study
from optuna import load_study
from optuna import version
from optuna.distributions import CategoricalDistribution
from optuna.distributions import UniformDistribution
from optuna.storages import RDBStorage
from optuna.storages._rdb.models import SCHEMA_VERSION
from optuna.storages._rdb.models import TrialHeartbeatModel
from optuna.storages._rdb.models import VersionInfoModel
from optuna.storages._rdb.storage import _create_scoped_session
from optuna.testing.storage import StorageSupplier
from optuna.testing.threading import _TestableThread
from optuna.trial import FrozenTrial
from optuna.trial import Trial
from optuna.trial import TrialState

from .create_db import mo_objective_test_upgrade
from .create_db import objective_test_upgrade


def test_init() -> None:

    storage = create_test_storage()
    session = storage.scoped_session()

    version_info = session.query(VersionInfoModel).first()
    assert version_info.schema_version == SCHEMA_VERSION
    assert version_info.library_version == version.__version__

    assert storage.get_current_version() == storage.get_head_version()
    assert storage.get_all_versions() == [
        "v2.6.0.a",
        "v2.4.0.a",
        "v1.3.0.a",
        "v1.2.0.a",
        "v0.9.0.a",
    ]


def test_init_url_template() -> None:

    with tempfile.NamedTemporaryFile(suffix="{SCHEMA_VERSION}") as tf:
        storage = RDBStorage("sqlite:///" + tf.name)
        assert storage.engine.url.database.endswith(str(SCHEMA_VERSION))


def test_init_url_that_contains_percent_character() -> None:

    # Alembic's ini file regards '%' as the special character for variable expansion.
    # We checks `RDBStorage` does not raise an error even if a storage url contains the character.
    with tempfile.NamedTemporaryFile(suffix="%") as tf:
        RDBStorage("sqlite:///" + tf.name)

    with tempfile.NamedTemporaryFile(suffix="%foo") as tf:
        RDBStorage("sqlite:///" + tf.name)

    with tempfile.NamedTemporaryFile(suffix="%foo%%bar") as tf:
        RDBStorage("sqlite:///" + tf.name)


def test_init_db_module_import_error() -> None:

    expected_msg = (
        "Failed to import DB access module for the specified storage URL. "
        "Please install appropriate one."
    )

    with patch.dict(sys.modules, {"psycopg2": None}):
        with pytest.raises(ImportError, match=expected_msg):
            RDBStorage("postgresql://user:password@host/database")


def test_engine_kwargs() -> None:

    create_test_storage(engine_kwargs={"pool_size": 5})

    with pytest.raises(TypeError):
        create_test_storage(engine_kwargs={"wrong_key": "wrong_value"})


@pytest.mark.parametrize(
    "url,engine_kwargs,expected",
    [
        ("mysql://localhost", {"pool_pre_ping": False}, False),
        ("mysql://localhost", {"pool_pre_ping": True}, True),
        ("mysql://localhost", {}, True),
        ("mysql+pymysql://localhost", {}, True),
        ("mysql://localhost", {"pool_size": 5}, True),
    ],
)
def test_set_default_engine_kwargs_for_mysql(
    url: str, engine_kwargs: Dict[str, Any], expected: bool
) -> None:

    RDBStorage._set_default_engine_kwargs_for_mysql(url, engine_kwargs)
    assert engine_kwargs["pool_pre_ping"] is expected


def test_set_default_engine_kwargs_for_mysql_with_other_rdb() -> None:

    # Do not change engine_kwargs if database is not MySQL.
    engine_kwargs: Dict[str, Any] = {}
    RDBStorage._set_default_engine_kwargs_for_mysql("sqlite:///example.db", engine_kwargs)
    assert "pool_pre_ping" not in engine_kwargs
    RDBStorage._set_default_engine_kwargs_for_mysql("postgres:///example.db", engine_kwargs)
    assert "pool_pre_ping" not in engine_kwargs


def test_check_table_schema_compatibility() -> None:

    storage = create_test_storage()
    session = storage.scoped_session()

    # The schema version of a newly created storage is always up-to-date.
    storage._version_manager.check_table_schema_compatibility()

    # `SCHEMA_VERSION` has not been used for compatibility check since alembic was introduced.
    version_info = session.query(VersionInfoModel).one()
    version_info.schema_version = SCHEMA_VERSION - 1
    session.commit()

    storage._version_manager.check_table_schema_compatibility()

    # TODO(ohta): Remove the following comment out when the second revision is introduced.
    # with pytest.raises(RuntimeError):
    #     storage._set_alembic_revision(storage._version_manager._get_base_version())
    #     storage._check_table_schema_compatibility()


def create_test_storage(engine_kwargs: Optional[Dict[str, Any]] = None) -> RDBStorage:

    storage = RDBStorage("sqlite:///:memory:", engine_kwargs=engine_kwargs)
    return storage


def test_pickle_storage() -> None:

    storage = create_test_storage()
    restored_storage = pickle.loads(pickle.dumps(storage))
    assert storage.url == restored_storage.url
    assert storage.engine_kwargs == restored_storage.engine_kwargs
    assert storage.skip_compatibility_check == restored_storage.skip_compatibility_check
    assert storage.engine != restored_storage.engine
    assert storage.scoped_session != restored_storage.scoped_session
    assert storage._version_manager != restored_storage._version_manager


def test_create_scoped_session() -> None:

    storage = create_test_storage()

    # This object violates the unique constraint of version_info_id.
    v = VersionInfoModel(version_info_id=1, schema_version=1, library_version="0.0.1")
    with pytest.raises(IntegrityError):
        with _create_scoped_session(storage.scoped_session) as session:
            session.add(v)


def test_upgrade_identity() -> None:

    storage = create_test_storage()

    # `upgrade()` has no effect because the storage version is already up-to-date.
    old_version = storage.get_current_version()
    storage.upgrade()
    new_version = storage.get_current_version()

    assert old_version == new_version


@pytest.mark.parametrize("optuna_version", ["0.9.0", "1.2.0", "1.3.0", "2.4.0", "2.6.0"])
def test_upgrade_single_objective_optimization(optuna_version: str) -> None:
    src_db_file = os.path.join(
        os.path.dirname(__file__), "test_upgrade_assets", f"{optuna_version}.db"
    )
    with tempfile.TemporaryDirectory() as workdir:
        shutil.copyfile(src_db_file, f"{workdir}/sqlite.db")
        storage_url = f"sqlite:///{workdir}/sqlite.db"

        storage = RDBStorage(storage_url, skip_compatibility_check=True)
        assert storage.get_current_version() == f"v{optuna_version}.a"
        head_version = storage.get_head_version()
        storage.upgrade()
        assert head_version == storage.get_current_version()

        # Create a new study.
        study = create_study(storage=storage)
        assert len(study.trials) == 0
        study.optimize(objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 1

        # Check empty study.
        study = load_study(storage=storage, study_name="single_empty")
        assert len(study.trials) == 0
        study.optimize(objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 1

        # Resume single objective optimization.
        study = load_study(storage=storage, study_name="single")
        assert len(study.trials) == 1
        study.optimize(objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 2
        for trial in study.trials:
            assert trial.system_attrs["a"] == 0
            assert trial.user_attrs["b"] == 1
            assert trial.intermediate_values[0] == 0.5
            assert -5 <= trial.params["x"] <= 5
            assert 0 <= trial.params["y"] <= 10
            assert trial.params["z"] in (-5, 0, 5)
            assert trial.value is not None and 0 <= trial.value <= 150

        assert study.system_attrs["c"] == 2
        assert study.user_attrs["d"] == 3


@pytest.mark.parametrize("optuna_version", ["2.4.0", "2.6.0"])
def test_upgrade_multi_objective_optimization(optuna_version: str) -> None:
    src_db_file = os.path.join(
        os.path.dirname(__file__), "test_upgrade_assets", f"{optuna_version}.db"
    )
    with tempfile.TemporaryDirectory() as workdir:
        shutil.copyfile(src_db_file, f"{workdir}/sqlite.db")
        storage_url = f"sqlite:///{workdir}/sqlite.db"

        storage = RDBStorage(storage_url, skip_compatibility_check=True)
        assert storage.get_current_version() == f"v{optuna_version}.a"
        head_version = storage.get_head_version()
        storage.upgrade()
        assert head_version == storage.get_current_version()

        # Create a new study.
        study = create_study(storage=storage, directions=["minimize", "minimize"])
        assert len(study.trials) == 0
        study.optimize(mo_objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 1

        # Check empty study.
        study = load_study(storage=storage, study_name="multi_empty")
        assert len(study.trials) == 0
        study.optimize(mo_objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 1

        # Resume multi-objective optimization.
        study = load_study(storage=storage, study_name="multi")
        assert len(study.trials) == 1
        study.optimize(mo_objective_test_upgrade, n_trials=1)
        assert len(study.trials) == 2
        for trial in study.trials:
            assert trial.system_attrs["a"] == 0
            assert trial.user_attrs["b"] == 1
            assert -5 <= trial.params["x"] <= 5
            assert 0 <= trial.params["y"] <= 10
            assert trial.params["z"] in (-5, 0, 5)
            assert -5 <= trial.values[0] < 5
            assert 0 <= trial.values[1] <= 150
        assert study.system_attrs["c"] == 2
        assert study.user_attrs["d"] == 3


@pytest.mark.parametrize(
    "fields_to_modify, kwargs",
    [
        (
            {"state": TrialState.COMPLETE, "datetime_complete": None},
            {"state": TrialState.COMPLETE},
        ),
        ({"_values": [1.1]}, {"values": [1.1]}),
        ({"_values": [1.1, 2.2]}, {"values": [1.1, 2.2]}),
        ({"intermediate_values": {1: 2.3, 3: 2.5}}, {"intermediate_values": {1: 2.3, 3: 2.5}}),
        (
            {
                "params": {"paramA": 3, "paramB": "bar"},
                "_distributions": {
                    "paramA": UniformDistribution(0, 3),
                    "paramB": CategoricalDistribution(("foo", "bar")),
                },
            },
            {
                "params": {
                    "paramA": UniformDistribution(0, 3).to_internal_repr(3),
                    "paramB": CategoricalDistribution(["foo", "bar"]).to_internal_repr("bar"),
                },
                "distributions_": {
                    "paramA": UniformDistribution(0, 3),
                    "paramB": CategoricalDistribution(["foo", "bar"]),
                },
            },
        ),
        (
            {"user_attrs": {"attrA": 2.3, "attrB": "foo"}},
            {"user_attrs": {"attrA": 2.3, "attrB": "foo"}},
        ),
        (
            {"system_attrs": {"attrC": 2.3, "attrB": "bar"}},
            {"system_attrs": {"attrC": 2.3, "attrB": "bar"}},
        ),
    ],
)
def test_update_trial(fields_to_modify: Dict[str, Any], kwargs: Dict[str, Any]) -> None:

    storage = create_test_storage()
    study_id = storage.create_new_study()

    trial_id = storage.create_new_trial(study_id)
    trial_before_update = storage.get_trial(trial_id)
    storage._update_trial(trial_id, **kwargs)
    trial_after_update = storage.get_trial(trial_id)
    for field in FrozenTrial._ordered_fields:
        if field not in fields_to_modify:
            assert getattr(trial_before_update, field) == getattr(trial_after_update, field)
        for key, value in fields_to_modify.items():
            if value is not None:
                assert getattr(trial_after_update, key) == value


@pytest.mark.parametrize("values1, values2", [([0.1], [1.1]), ([0.1, 0.2], [1.1, 1.2])])
def test_update_trial_second_write(values1: List[float], values2: List[float]) -> None:

    storage = create_test_storage()
    study_id = storage.create_new_study()
    template = FrozenTrial(
        number=1,
        state=TrialState.RUNNING,
        value=None,
        values=values1,
        datetime_start=None,
        datetime_complete=None,
        params={"paramA": 0.1, "paramB": 1.1},
        distributions={"paramA": UniformDistribution(0, 1), "paramB": UniformDistribution(0, 2)},
        user_attrs={"userA": 2, "userB": 3},
        system_attrs={"sysA": 4, "sysB": 5},
        intermediate_values={3: 1.2, 5: 9.2},
        trial_id=1,
    )
    trial_id = storage.create_new_trial(study_id, template)
    trial_before_update = storage.get_trial(trial_id)
    storage._update_trial(
        trial_id,
        state=None,
        values=values2,
        intermediate_values={3: 2.3, 7: 3.3},
        params={"paramA": 0.2, "paramC": 2.3},
        distributions_={"paramA": UniformDistribution(0, 1), "paramC": UniformDistribution(0, 4)},
        user_attrs={"userA": 1, "userC": "attr"},
        system_attrs={"sysA": 6, "sysC": 8},
    )
    trial_after_update = storage.get_trial(trial_id)
    expected_attrs = {
        "_trial_id": trial_before_update._trial_id,
        "number": trial_before_update.number,
        "state": TrialState.RUNNING,
        "values": values2,
        "params": {"paramA": 0.2, "paramB": 1.1, "paramC": 2.3},
        "intermediate_values": {3: 2.3, 5: 9.2, 7: 3.3},
        "_distributions": {
            "paramA": UniformDistribution(0, 1),
            "paramB": UniformDistribution(0, 2),
            "paramC": UniformDistribution(0, 4),
        },
        "user_attrs": {"userA": 1, "userB": 3, "userC": "attr"},
        "system_attrs": {"sysA": 6, "sysB": 5, "sysC": 8},
    }
    for key, value in expected_attrs.items():
        assert getattr(trial_after_update, key) == value


def test_get_trials_excluded_trial_ids() -> None:

    storage = create_test_storage()
    study_id = storage.create_new_study()

    storage.create_new_trial(study_id)

    trials = storage._get_trials(study_id, states=None, excluded_trial_ids=set())
    assert len(trials) == 1

    # A large exclusion list used to raise errors. Check that it is not an issue.
    # See https://github.com/optuna/optuna/issues/1457.
    trials = storage._get_trials(study_id, states=None, excluded_trial_ids=set(range(500000)))
    assert len(trials) == 0


def test_record_heartbeat() -> None:

    heartbeat_interval = 1
    n_trials = 2
    sleep_sec = 2

    def objective(trial: Trial) -> float:
        time.sleep(sleep_sec)
        return 1.0

    with StorageSupplier("sqlite") as storage:
        assert isinstance(storage, RDBStorage)
        storage.heartbeat_interval = heartbeat_interval
        study = create_study(storage=storage)
        # Exceptions raised in spawned threads are caught by `_TestableThread`.
        with patch("optuna.study._optimize.Thread", _TestableThread):
            study.optimize(objective, n_trials=n_trials)

        trial_heartbeats = []

        with _create_scoped_session(storage.scoped_session) as session:
            trial_ids = [trial._trial_id for trial in study.trials]
            for trial_id in trial_ids:
                heartbeat_model = TrialHeartbeatModel.where_trial_id(trial_id, session)
                assert heartbeat_model is not None
                trial_heartbeats.append(heartbeat_model.heartbeat)

        assert len(trial_heartbeats) == n_trials
        trials = study.trials
        for i in range(n_trials - 1):
            datetime_start = trials[i + 1].datetime_start
            prev_datetime_complete = trials[i].datetime_complete
            assert datetime_start is not None and prev_datetime_complete is not None
            trial_prep = (datetime_start - prev_datetime_complete).seconds
            heartbeats_interval = (trial_heartbeats[i + 1] - trial_heartbeats[i]).seconds
            assert heartbeats_interval - sleep_sec - trial_prep <= 1
