from __future__ import annotations

from sqlalchemy import select

from app.database.models import TradeSignalRecord
from app.database.repository import Repository
from app.trading.models import MessageStatus
from app.trading.processor import SignalProcessor
from tests.fixtures import EXAMPLE_A, EXAMPLE_C, ZONE_BUY


def test_same_telegram_message_not_processed_twice(
    processor: SignalProcessor, repository: Repository
) -> None:
    first = processor.handle_new_message(1001, 82911, ZONE_BUY)
    second = processor.handle_new_message(1001, 82911, ZONE_BUY)
    assert first is not None
    assert second is None
    stored = repository.get_message(1001, 82911)
    assert stored is not None
    assert stored.status == MessageStatus.WAITING_ENTRY.value
    assert repository.get_setup(1001, 82911) is not None


def test_same_prices_are_separate_setups(
    processor: SignalProcessor, repository: Repository
) -> None:
    first = processor.handle_new_message(1001, 10, ZONE_BUY)
    second = processor.handle_new_message(1001, 11, ZONE_BUY)
    assert first is not None
    assert second is not None
    assert second.validation_status.value == "PARSED"
    assert repository.get_setup(1001, 10) is not None
    assert repository.get_setup(1001, 11) is not None


def test_unauthorized_channel_ignored(
    processor: SignalProcessor, repository: Repository
) -> None:
    result = processor.handle_new_message(9999, 1, EXAMPLE_A)
    assert result is None
    stored = repository.get_message(9999, 1)
    assert stored is not None
    assert stored.status == MessageStatus.IGNORED.value


def test_forwarded_is_recorded(
    processor: SignalProcessor, repository: Repository
) -> None:
    signal = processor.handle_new_message(
        1001, 20, EXAMPLE_A, is_forwarded=True
    )
    assert signal is not None
    assert signal.is_forwarded is True
    stored = repository.get_message(1001, 20)
    assert stored is not None
    assert stored.is_forwarded is True


def test_edit_reparses_when_not_executed(processor: SignalProcessor) -> None:
    processor.handle_new_message(1001, 30, EXAMPLE_A)
    updated = processor.handle_edit(1001, 30, EXAMPLE_C)
    assert updated is not None
    assert updated.direction is not None
    assert updated.direction.value == "SELL"


def test_edit_after_execute_does_not_create_trade(
    processor: SignalProcessor, repository: Repository
) -> None:
    processor.handle_new_message(1001, 31, EXAMPLE_A)
    with repository._session_factory() as session:
        row = session.scalars(
            select(TradeSignalRecord).where(
                TradeSignalRecord.telegram_channel_id == 1001,
                TradeSignalRecord.telegram_message_id == 31,
            )
        ).first()
        assert row is not None
        row.execution_status = "EXECUTED"
        session.commit()
    result = processor.handle_edit(1001, 31, EXAMPLE_C)
    assert result is None
    message = repository.get_message(1001, 31)
    assert message is not None
    assert message.status == MessageStatus.EDITED_AFTER_EXECUTE.value


def test_delete_cancels_pending(
    processor: SignalProcessor, repository: Repository
) -> None:
    processor.handle_new_message(1001, 40, EXAMPLE_A)
    processor.handle_delete(1001, 40)
    stored = repository.get_message(1001, 40)
    assert stored is not None
    assert stored.status == MessageStatus.DELETED.value
    assert stored.is_deleted is True
