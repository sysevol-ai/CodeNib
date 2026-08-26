# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from collections import ChainMap
from collections.abc import Mapping
from pathlib import Path

import pytest

import codenib.index.embedding.model_policy as model_policy_module
from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
    resolve_embedding_artifact_load_policy,
    resolve_embedding_artifact_load_policy_from_options,
    resolve_embedding_load_policy,
)


def test_bundled_model_uses_pinned_trusted_revision_by_default():
    policy = resolve_embedding_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision == DEFAULT_EMBEDDING_REVISION
    assert policy.trust_remote_code is True


def test_arbitrary_model_remains_untrusted_by_default():
    policy = resolve_embedding_load_policy("vendor/custom-embedding")

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_custom_revision_of_bundled_model_is_not_implicitly_trusted():
    policy = resolve_embedding_load_policy(
        DEFAULT_EMBEDDING_MODEL,
        revision="caller-controlled-revision",
    )

    assert policy.revision == "caller-controlled-revision"
    assert policy.trust_remote_code is False


def test_caller_can_explicitly_override_remote_code_policy():
    enabled = resolve_embedding_load_policy(
        "vendor/custom-embedding",
        revision="a" * 40,
        trust_remote_code=True,
    )
    disabled = resolve_embedding_load_policy(
        DEFAULT_EMBEDDING_MODEL,
        trust_remote_code=False,
    )

    assert enabled.trust_remote_code is True
    assert disabled.revision == DEFAULT_EMBEDDING_REVISION
    assert disabled.trust_remote_code is False


@pytest.mark.parametrize(
    "revision",
    [None, "main", "a" * 39, "g" * 40],
)
def test_remote_code_requires_full_immutable_revision(revision):
    with pytest.raises(ValueError, match="full 40-character lowercase commit SHA"):
        resolve_embedding_load_policy(
            "vendor/custom-embedding",
            revision=revision,
            trust_remote_code=True,
        )


def test_local_model_can_trust_local_code_without_revision(tmp_path):
    model_path = tmp_path / "model"
    model_path.mkdir()

    policy = resolve_embedding_load_policy(
        str(model_path),
        trust_remote_code=True,
    )

    assert policy.revision is None
    assert policy.trust_remote_code is True


def test_local_path_named_like_bundled_model_is_not_implicitly_trusted(
    tmp_path, monkeypatch
):
    model_path = tmp_path / DEFAULT_EMBEDDING_MODEL
    model_path.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    policy = resolve_embedding_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_artifact_policy_treats_model_id_as_remote_without_filesystem_probe(
    tmp_path, monkeypatch
):
    local_collision = tmp_path / "vendor" / "custom-embedding"
    local_collision.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def fail_is_dir(_path: Path) -> bool:
        pytest.fail("artifact embedding policy probed the ambient filesystem")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)

    policy = resolve_embedding_artifact_load_policy("vendor/custom-embedding")

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_artifact_policy_keeps_bundled_remote_identity_deterministic(
    tmp_path, monkeypatch
):
    local_collision = tmp_path / DEFAULT_EMBEDDING_MODEL
    local_collision.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def fail_is_dir(_path: Path) -> bool:
        pytest.fail("artifact embedding policy probed the ambient filesystem")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)

    policy = resolve_embedding_artifact_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision == DEFAULT_EMBEDDING_REVISION
    assert policy.trust_remote_code is True


@pytest.mark.parametrize(
    "model",
    [
        "/opt/models/embedding",
        "C:/models/embedding",
        "C:\\models\\embedding",
        "C:embedding",
        "//server/share/embedding",
        "\\\\server\\share\\embedding",
        "./embedding",
        "../embedding",
        "vendor/../embedding",
        "~/embedding",
        "~other/embedding",
        "vendor\\embedding",
        "vendor//embedding",
        "file:/opt/models/embedding",
        "FILE:relative-model",
        "vendor/model name",
        "vendor/model\x7f",
    ],
)
def test_artifact_policy_rejects_filesystem_shaped_models(model):
    with pytest.raises(ValueError, match="filesystem-shaped path"):
        resolve_embedding_artifact_load_policy(model)


@pytest.mark.parametrize("value", [1, "false"])
def test_remote_code_option_requires_boolean(value):
    with pytest.raises(TypeError, match="must be a bool or None"):
        resolve_embedding_load_policy(
            "vendor/custom-embedding",
            trust_remote_code=value,
        )


def test_artifact_model_callback_polls_long_scalar_after_current_unit() -> None:
    stop = KeyboardInterrupt("injected artifact model stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_embedding_artifact_load_policy(
            "vendor/" + "m" * (16 * 1024 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    with pytest.raises(ValueError, match="filesystem-shaped") as current:
        resolve_embedding_artifact_load_policy(
            "vendor/bad model/" + "m" * (16 * 1024 * 1024),
            check_cancelled=check_cancelled,
        )
    assert current.value is not stop


def test_artifact_model_callback_preserves_exact_stop_iteration() -> None:
    stop = StopIteration("injected artifact model iteration stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy(
            "vendor/" + "m" * (128 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


def test_artifact_model_callback_preserves_casefolded_file_scheme() -> None:
    model = "\ufb01le:remote"
    for callback in (None, lambda: None):
        with pytest.raises(ValueError, match="filesystem-shaped"):
            resolve_embedding_artifact_load_policy(
                model,
                check_cancelled=callback,
            )


def test_artifact_policy_uses_builtin_revision_length_semantics() -> None:
    class Revision(str):
        def __len__(self) -> int:
            return 41

    revision = Revision("a" * 40)
    legacy = resolve_embedding_artifact_load_policy(
        "vendor/model",
        revision=revision,
        trust_remote_code=True,
    )
    callback = resolve_embedding_artifact_load_policy(
        "vendor/model",
        revision=revision,
        trust_remote_code=True,
        check_cancelled=lambda: None,
    )
    assert callback == legacy


def test_artifact_policy_rejects_huge_revision_before_regex_scan() -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        resolve_embedding_artifact_load_policy(
            "vendor/model",
            revision="a" * (16 * 1024 * 1024),
            trust_remote_code=True,
            check_cancelled=lambda: None,
        )


def test_artifact_model_none_callback_keeps_legacy_helper_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_classifier = model_policy_module._filesystem_shaped_artifact_model
    calls = 0

    def legacy_classifier(model: str) -> bool:
        nonlocal calls
        calls += 1
        return real_classifier(model)

    monkeypatch.setattr(
        model_policy_module,
        "_filesystem_shaped_artifact_model",
        legacy_classifier,
    )
    policy = resolve_embedding_artifact_load_policy("vendor/model")

    assert policy.trust_remote_code is False
    assert calls == 1


def test_artifact_option_duplicate_scalar_comparison_is_interruptible() -> None:
    revision = "".join(("a" * (64 * 1024), "a" * (64 * 1024)))
    nested_revision = "".join(("a" * (64 * 1024), "a" * (64 * 1024)))
    assert revision is not nested_revision
    stop = RuntimeError("injected model-policy option stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == 2:
            raise stop

    with pytest.raises(RuntimeError) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {
                "revision": revision,
                "model_kwargs": {"revision": nested_revision},
            },
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    polls = 0
    with pytest.raises(ValueError, match="conflicting revision values") as current:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {
                "revision": "b" + revision[1:],
                "model_kwargs": {"revision": nested_revision},
            },
            check_cancelled=check_cancelled,
        )
    assert current.value is not stop


def test_artifact_option_current_invalid_precedes_armed_future_stop() -> None:
    stop = KeyboardInterrupt("injected model-option future stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(TypeError, match="revision must be a string") as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {"revision": object(), "future": 1},
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop


@pytest.mark.parametrize("value", [[], (), "", b"", 0, False, set()])
def test_artifact_option_callback_preserves_legacy_falsy_model_kwargs(
    value: object,
) -> None:
    assert resolve_embedding_artifact_load_policy_from_options(
        "vendor/model",
        {"model_kwargs": value},
        check_cancelled=lambda: None,
    ) == resolve_embedding_artifact_load_policy_from_options(
        "vendor/model",
        {"model_kwargs": value},
    )


def test_artifact_option_callback_preserves_falsey_mapping_semantics() -> None:
    class FalseyOptions(dict[str, object]):
        def __bool__(self) -> bool:
            return False

    revision = "a" * 40
    for options in (
        FalseyOptions(revision=revision),
        {"model_kwargs": FalseyOptions(revision=revision)},
    ):
        assert resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            options,
            check_cancelled=lambda: None,
        ) == resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            options,
        )


def test_artifact_option_polls_after_lazy_key_before_value_read() -> None:
    stop = StopIteration("injected lazy model-option stop")
    armed = False
    value_touched = False

    class LazyOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("model options touched a poisoned future value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield "revision"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            LazyOptions(),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not value_touched


def test_artifact_option_callback_preserves_selective_nested_get_semantics() -> None:
    selected_revision = "b" * 40

    class GetOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            assert key == "revision"
            return "a" * 40

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "revision"

        def __len__(self) -> int:
            return 1

        def get(self, key: str, default: object = None) -> object:
            if key == "revision":
                return selected_revision
            return default

    options = {"model_kwargs": GetOptions()}
    legacy = resolve_embedding_artifact_load_policy_from_options(
        "vendor/model",
        options,
    )
    callback = resolve_embedding_artifact_load_policy_from_options(
        "vendor/model",
        options,
        check_cancelled=lambda: None,
    )
    assert callback == legacy
    assert callback.revision == selected_revision


def test_artifact_option_callback_does_not_scan_irrelevant_nested_mapping() -> None:
    class SelectiveOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise AssertionError("model policy touched an irrelevant nested value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("model policy scanned irrelevant nested keys")

        def __len__(self) -> int:
            return 1

        def get(self, key: str, default: object = None) -> object:
            return default

    options = {"model_kwargs": SelectiveOptions()}
    assert resolve_embedding_artifact_load_policy_from_options(
        "vendor/model",
        options,
        check_cancelled=lambda: None,
    ) == resolve_embedding_artifact_load_policy_from_options("vendor/model", options)


def test_artifact_option_polls_after_nested_truth_before_get() -> None:
    stop = StopIteration("injected nested-get future stop")
    armed = False
    get_touched = False

    class ArmedOptions(Mapping[str, object]):
        def __bool__(self) -> bool:
            nonlocal armed
            armed = True
            return True

        def __getitem__(self, key: str) -> object:
            raise AssertionError("nested get fell through to a poisoned value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(())

        def __len__(self) -> int:
            return 1

        def get(self, key: str, default: object = None) -> object:
            nonlocal get_touched
            get_touched = True
            raise AssertionError("model policy entered an armed nested get")

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {"model_kwargs": ArmedOptions()},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not get_touched


def test_artifact_option_prefix_reconciliation_avoids_nested_future_protocol() -> None:
    stop = StopIteration("injected outer model-option stop")
    truth_touched = False

    class PoisonedNested(Mapping[str, object]):
        def __bool__(self) -> bool:
            nonlocal truth_touched
            truth_touched = True
            raise AssertionError("prefix reconciliation touched nested truthiness")

        def __getitem__(self, key: str) -> object:
            raise AssertionError("prefix reconciliation touched a nested value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("prefix reconciliation scanned nested keys")

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {"model_kwargs": PoisonedNested(), "future": 1},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not truth_touched


def test_artifact_option_prefix_reconciliation_avoids_hostile_key_equality() -> None:
    stop = StopIteration("injected hostile outer-key stop")
    equality_touched = False

    class HostileKey(str):
        def __hash__(self) -> int:
            return hash("model_kwargs")

        def __eq__(self, other: object) -> bool:
            nonlocal equality_touched
            equality_touched = True
            raise AssertionError("prefix reconciliation compared a caller key")

    options = {HostileKey("irrelevant"): 1, "future": 2}

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            options,
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not equality_touched


def test_artifact_option_prefix_reconciliation_discards_aliased_side_state() -> None:
    stop = StopIteration("injected aliased model-option stop")
    armed = False
    invalid_revision = object()

    class Alias(str):
        pass

    alias = Alias("revision")

    class AliasedOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal armed
            if type(key) is str:
                return invalid_revision
            assert key is alias
            armed = True
            return "a" * 40

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "revision"
            yield alias

        def __len__(self) -> int:
            return 2

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            AliasedOptions(),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


def test_artifact_option_current_nested_candidate_precedes_future_get_stop() -> None:
    stop = StopIteration("injected post-revision nested-get stop")
    armed = False

    class CurrentInvalidOptions(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            nonlocal armed
            if key == "revision":
                armed = True
                return object()
            raise AssertionError("model policy touched future trust option")

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(TypeError, match="revision must be a string") as caught:
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            {"model_kwargs": CurrentInvalidOptions(value=1)},
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop


def test_artifact_option_callback_preserves_conflict_before_type_order() -> None:
    cases = [
        {
            "trust_remote_code": False,
            "model_kwargs": {"trust_remote_code": 0},
        },
        {"revision": 1, "model_kwargs": {"revision": False}},
    ]
    for options in cases:
        legacy_error: BaseException | None = None
        callback_error: BaseException | None = None
        try:
            legacy = resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                options,
            )
        except BaseException as error:  # noqa: B036 - compare public outcomes
            legacy_error = error
            legacy = None
        try:
            callback = resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                options,
                check_cancelled=lambda: None,
            )
        except BaseException as error:  # noqa: B036 - compare public outcomes
            callback_error = error
            callback = None
        assert type(callback_error) is type(legacy_error)
        assert callback == legacy


def test_artifact_option_callback_preserves_cross_field_conflict_order() -> None:
    options = {
        "model_kwargs": ChainMap(
            {
                "revision": 1,
                "trust_remote_code": False,
            }
        ),
        "trust_remote_code": {},
    }
    for callback in (None, lambda: None):
        with pytest.raises(ValueError, match="conflicting trust_remote_code"):
            resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                options,
                check_cancelled=callback,
            )


@pytest.mark.parametrize("equal,not_equal", [(True, True), (False, False)])
def test_artifact_option_callback_preserves_custom_inequality_semantics(
    equal: bool,
    not_equal: bool,
) -> None:
    class Weird:
        def __eq__(self, other: object) -> bool:
            return equal

        def __ne__(self, other: object) -> bool:
            return not_equal

    direct = Weird()
    nested = Weird()
    options = {"revision": direct, "model_kwargs": {"revision": nested}}
    outcomes: list[type[BaseException] | None] = []
    for callback in (None, lambda: None):
        try:
            resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                options,
                check_cancelled=callback,
            )
        except BaseException as error:  # noqa: B036 - compare legacy outcomes
            outcomes.append(type(error))
        else:  # pragma: no cover - invalid candidate must fail
            outcomes.append(None)
    assert outcomes[0] is outcomes[1]


def test_artifact_option_callback_preserves_nested_subclass_equality() -> None:
    class UnequalText(str):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    class UnequalBytes(bytes):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    class UnequalList(list[object]):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    class UnequalTuple(tuple[object, ...]):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    class UnequalDict(dict[str, object]):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    factories = (
        lambda: UnequalText("value"),
        lambda: UnequalBytes(b"value"),
        lambda: UnequalList([1]),
        lambda: UnequalTuple((1,)),
        lambda: UnequalDict(value=1),
    )
    for factory in factories:
        options = {
            "revision": [factory()],
            "model_kwargs": {"revision": [factory()]},
        }
        for callback in (None, lambda: None):
            with pytest.raises(ValueError, match="conflicting revision"):
                resolve_embedding_artifact_load_policy_from_options(
                    "vendor/model",
                    options,
                    check_cancelled=callback,
                )


def test_artifact_option_dict_equality_polls_before_hostile_fallback_lookup() -> None:
    stop = StopIteration("injected dict-equality fallback stop")
    armed = False
    poison_touched = False

    class HostileKey:
        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            nonlocal armed, poison_touched
            if not armed:
                armed = True
                return False
            poison_touched = True
            raise AssertionError("dict equality entered a poisoned fallback lookup")

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        model_policy_module._policy_values_equal_interruptibly(
            {HostileKey(): 1},
            {HostileKey(): 1},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not poison_touched


def test_artifact_option_duplicate_large_containers_are_interruptible() -> None:
    stop = StopIteration("injected model-option equality stop")
    direct = list(range(100_000))
    nested = list(range(100_000))
    armed = False

    def check_cancelled() -> None:
        if armed:
            raise stop

    real_equal = model_policy_module._policy_values_equal_interruptibly

    def arm_equal(left: object, right: object, **kwargs: object) -> bool:
        nonlocal armed
        armed = True
        return real_equal(left, right, **kwargs)  # type: ignore[arg-type]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        model_policy_module,
        "_policy_values_equal_interruptibly",
        arm_equal,
    )
    try:
        with pytest.raises(StopIteration) as caught:
            resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                {"revision": direct, "model_kwargs": {"revision": nested}},
                check_cancelled=check_cancelled,
            )
        assert caught.value is stop
    finally:
        monkeypatch.undo()


def test_artifact_option_duplicate_large_dict_comparison_is_interruptible() -> None:
    stop = StopIteration("injected model-option dict equality stop")
    for mismatch in (False, True):
        direct = {str(index): index for index in range(100_000)}
        nested = {str(index): index for index in range(100_000)}
        if mismatch:
            direct["0"] = -1
        polls = 0

        def check_cancelled() -> None:
            nonlocal polls
            polls += 1
            if polls == 2:
                raise stop

        if mismatch:
            with pytest.raises(ValueError, match="conflicting revision") as caught:
                resolve_embedding_artifact_load_policy_from_options(
                    "vendor/model",
                    {"revision": direct, "model_kwargs": {"revision": nested}},
                    check_cancelled=check_cancelled,
                )
            assert caught.value is not stop
            continue
        with pytest.raises(StopIteration) as caught:
            resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                {"revision": direct, "model_kwargs": {"revision": nested}},
                check_cancelled=check_cancelled,
            )
        assert caught.value is stop


def test_artifact_option_callback_preserves_nonreflexive_equality() -> None:
    nan = float("nan")
    top_level = {"revision": nan, "model_kwargs": {"revision": nan}}
    with pytest.raises(ValueError, match="conflicting revision"):
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            top_level,
        )
    with pytest.raises(ValueError, match="conflicting revision"):
        resolve_embedding_artifact_load_policy_from_options(
            "vendor/model",
            top_level,
            check_cancelled=lambda: None,
        )

    for direct, nested in (
        ([nan], [nan]),
        ((nan,), (nan,)),
        ({"value": nan}, {"value": nan}),
    ):
        options = {"revision": direct, "model_kwargs": {"revision": nested}}
        with pytest.raises(TypeError) as legacy:
            resolve_embedding_artifact_load_policy_from_options("vendor/model", options)
        with pytest.raises(type(legacy.value)):
            resolve_embedding_artifact_load_policy_from_options(
                "vendor/model",
                options,
                check_cancelled=lambda: None,
            )


@pytest.mark.parametrize(
    "resolver,args",
    [
        (resolve_embedding_artifact_load_policy, ("vendor/model",)),
        (
            resolve_embedding_artifact_load_policy_from_options,
            ("vendor/model", {}),
        ),
    ],
)
def test_artifact_policy_rejects_noncallable_callback(
    resolver: object,
    args: tuple[object, ...],
) -> None:
    with pytest.raises(TypeError, match="callable"):
        resolver(*args, check_cancelled=17)  # type: ignore[operator]
