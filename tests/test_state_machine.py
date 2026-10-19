import pytest

from finkraft.state_machine import TERMINAL, TRANSITIONS, InvalidTransition, State, validate_transition


def test_happy_path_is_allowed():
    path = [State.RECEIVED, State.READY, State.SUBMITTING, State.SUBMITTED,
            State.ASP_ACCEPTED, State.DELIVERED, State.REPORTED]
    for src, dst in zip(path, path[1:]):
        validate_transition(src, dst)


@pytest.mark.parametrize("src,dst", [
    (State.RECEIVED, State.SUBMITTING),     # must be validated first
    (State.RECEIVED, State.REPORTED),
    (State.READY, State.DELIVERED),         # can't skip submission
    (State.REPORTED, State.DELIVERED),      # no going backwards
    (State.DELIVERED, State.ASP_ACCEPTED),
    (State.DELIVERED, State.ASP_REJECTED),  # contradictory: needs manual review, not a transition
    (State.ASP_ACCEPTED, State.SUBMITTED),
    (State.SUBMIT_FAILED, State.SUBMITTING),  # must go through READY (single claim point)
])
def test_invalid_transitions_rejected(src, dst):
    with pytest.raises(InvalidTransition):
        validate_transition(src, dst)


def test_terminal_states_have_no_exits():
    assert TERMINAL == {State.VALIDATION_FAILED, State.ASP_REJECTED, State.REPORTED}
    for s in TERMINAL:
        assert TRANSITIONS[s] == frozenset()


def test_every_state_is_covered():
    assert set(TRANSITIONS) == set(State)
