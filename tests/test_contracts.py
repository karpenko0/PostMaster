"""Внутренние интерфейсы из §5 SPEC-001 существуют, Publisher абстрактен (BR-03; п. 5.5 плана)."""

import inspect
from typing import Any

import pytest

from fake_telegram import TEST_TOKEN
from postmaster.bot.factory import create_bot
from postmaster.bot.transport import PollingTransport, Transport
from postmaster.publishers.base import Publisher
from postmaster.services.post_service import PostService
from postmaster.services.publication_service import PublicationService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService


def test_internal_service_contracts_exist() -> None:
    for service in (PostService, PublicationService, UserService, SchedulerService):
        assert inspect.isclass(service)


def test_scheduler_service_exposes_lifecycle() -> None:
    scheduler = SchedulerService()

    assert scheduler.running is False
    assert callable(scheduler.start)
    assert callable(scheduler.shutdown)


def test_publisher_is_abstract() -> None:
    assert inspect.isabstract(Publisher)
    assert inspect.iscoroutinefunction(Publisher.publish)
    with pytest.raises(TypeError):
        Publisher()  # type: ignore[abstract]


async def test_concrete_publisher_implements_publish() -> None:
    class RecordingPublisher(Publisher):
        def __init__(self) -> None:
            self.published: list[Any] = []

        async def publish(self, post: Any) -> None:
            self.published.append(post)

    publisher = RecordingPublisher()
    await publisher.publish("post-1")

    assert publisher.published == ["post-1"]


def test_polling_transport_satisfies_transport_protocol() -> None:
    transport = PollingTransport(create_bot(TEST_TOKEN))

    assert isinstance(transport, Transport)
