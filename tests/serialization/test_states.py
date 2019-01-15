import base64
import datetime
import json

import cloudpickle
import marshmallow
import pendulum
import pytest

import prefect
from prefect.engine.result_handlers import ResultHandler
from prefect.engine import state
from prefect.serialization.state import ResultHandlerField, StateSchema

all_states = sorted(
    set(
        cls
        for cls in state.__dict__.values()
        if isinstance(cls, type)
        and issubclass(cls, state.State)
        and cls is not state.State
    ),
    key=lambda c: c.__name__,
)


def complex_states():
    naive_dt = datetime.datetime(2020, 1, 1)
    utc_dt = pendulum.datetime(2020, 1, 1)
    complex_result = {"x": 1, "y": {"z": 2}}
    cached_state = state.CachedState(
        cached_inputs=complex_result,
        cached_result=complex_result,
        cached_parameters=complex_result,
        cached_result_expiration=utc_dt,
    )
    cached_state_naive = state.CachedState(
        cached_inputs=complex_result,
        cached_result=complex_result,
        cached_parameters=complex_result,
        cached_result_expiration=naive_dt,
    )
    test_states = [
        state.Pending(cached_inputs=complex_result),
        state.Paused(cached_inputs=complex_result),
        state.Retrying(start_time=utc_dt, run_count=3),
        state.Retrying(start_time=naive_dt, run_count=3),
        state.Scheduled(start_time=utc_dt),
        state.Scheduled(start_time=naive_dt),
        state.Resume(start_time=utc_dt),
        state.Resume(start_time=naive_dt),
        state.Submitted(state=state.Retrying(start_time=utc_dt, run_count=2)),
        state.Submitted(state=state.Resume(start_time=utc_dt)),
        cached_state,
        cached_state_naive,
        state.Success(result=complex_result, cached=cached_state),
        state.Success(result=complex_result, cached=cached_state_naive),
        state.TimedOut(cached_inputs=complex_result),
    ]
    return test_states


def test_all_states_have_serialization_schemas_in_stateschema():
    """
    Tests that all State subclasses in prefect.engine.states have corresponding schemas
    in prefect.serialization.state
    """
    assert set(s.__name__ for s in all_states) == set(StateSchema.type_schemas.keys())


def test_all_states_have_deserialization_schemas_in_stateschema():
    """
    Tests that all State subclasses in prefect.engine.states have corresponding schemas
    in prefect.serialization.state with that state assigned as the object class
    so it will be recreated at deserialization
    """
    assert set(all_states) == set(
        s.Meta.object_class for s in StateSchema.type_schemas.values()
    )


class AddOneHandler(ResultHandler):
    def serialize(self, result):
        return str(result - 1)

    def deserialize(self, result):
        return int(result) + 1


class PickleHandler(ResultHandler):
    def serialize(self, result):
        return base64.b64encode(cloudpickle.dumps(result)).decode()

    def deserialize(self, result):
        return cloudpickle.loads(base64.b64decode(result.encode()))


class TestResultHandlerField:
    class Schema(marshmallow.Schema):
        field = ResultHandlerField()

    def test_initializes_and_calls_result_handler_for_serialization(self):
        schema = self.Schema(context={"result_handler": AddOneHandler()})
        serialized = schema.dump({"field": 50})
        assert "field" in serialized
        assert serialized["field"] == "49"

    def test_initializes_and_calls_result_handler_for_deserialization(self):
        schema = self.Schema(context={"result_handler": AddOneHandler()})
        deserialized = schema.load({"field": "49"})
        assert "field" in deserialized
        assert deserialized["field"] == 50

    def test_doesnt_require_result_handler_for_serialization(self):
        schema = self.Schema()
        serialized = schema.dump({"field": 50})
        assert "field" in serialized
        assert serialized["field"] == 50

    def test_doesnt_require_result_handler_for_deserialization(self):
        schema = self.Schema()
        deserialized = schema.load({"field": "49"})
        assert "field" in deserialized
        assert deserialized["field"] == "49"

    def test_non_json_compatible_result_handler(self):
        schema = self.Schema(context={"result_handler": PickleHandler()})
        serialized = schema.dump({"field": (lambda: 1)})
        assert isinstance(serialized["field"], str)

        deserialized = schema.load(serialized)
        assert "field" in deserialized
        assert deserialized["field"]() == 1


@pytest.mark.parametrize("cls", [s for s in all_states if s is not state.Mapped])
def test_serialize_state(cls):
    serialized = StateSchema().dump(cls(message="message", result=1))
    assert isinstance(serialized, dict)
    assert serialized["type"] == cls.__name__
    assert serialized["message"] is "message"
    assert serialized["result"] == 1
    assert serialized["__version__"] == prefect.__version__


def test_serialize_mapped():
    s = state.Success(message="1", result=1)
    f = state.Failed(message="2", result=2)
    serialized = StateSchema().dump(state.Mapped(message="message", map_states=[s, f]))
    assert isinstance(serialized, dict)
    assert serialized["type"] == "Mapped"
    assert serialized["message"] is "message"
    assert "result" not in serialized
    assert "map_states" not in serialized
    assert serialized["n_map_states"] == 2
    assert serialized["__version__"] == prefect.__version__


@pytest.mark.parametrize("cls", [s for s in all_states if s is not state.Mapped])
def test_deserialize_state(cls):
    s = cls(message="message", result=1)
    serialized = StateSchema().dump(s)
    deserialized = StateSchema().load(serialized)
    assert isinstance(deserialized, cls)
    assert deserialized == s


def test_deserialize_mapped():
    s = state.Success(message="1", result=1)
    f = state.Failed(message="2", result=2)
    serialized = StateSchema().dump(state.Mapped(message="message", map_states=[s, f]))
    deserialized = StateSchema().load(serialized)
    assert isinstance(deserialized, state.Mapped)
    assert len(deserialized.map_states) == 2
    assert all([isinstance(s, state.Pending) for s in deserialized.map_states])
    assert deserialized.result == None


@pytest.mark.parametrize("cls", all_states)
def test_deserialize_state_from_only_type(cls):
    serialized = dict(type=cls.__name__)
    new_state = StateSchema().load(serialized)
    assert isinstance(new_state, cls)
    assert new_state.message is None
    assert new_state.result is None


def test_deserialize_state_without_type_fails():
    with pytest.raises(marshmallow.exceptions.ValidationError):
        StateSchema().load({})


def test_deserialize_state_with_unknown_type_fails():
    with pytest.raises(marshmallow.exceptions.ValidationError):
        StateSchema().load({"type": "FakeState"})


@pytest.mark.parametrize("state", complex_states())
def test_complex_state_attributes_are_handled(state):
    serialized = StateSchema().dump(state)
    deserialized = StateSchema().load(serialized)
    assert state == deserialized


def test_result_must_be_valid_json():
    s = state.Success(result={"x": {"y": {"z": 1}}})
    serialized = StateSchema().dump(s)
    assert serialized["result"] == s.result


def test_result_raises_error_on_dump_if_not_valid_json():
    s = state.Success(result={"x": {"y": {"z": lambda: 1}}})
    with pytest.raises(TypeError):
        StateSchema().dump(s)


def test_deserialize_json_without_version():
    deserialized = StateSchema().load(
        {"type": "Running", "message": "test", "result": 1}
    )
    assert type(deserialized) is state.Running
    assert deserialized.is_running()
    assert deserialized.message == "test"
    assert deserialized.result == 1