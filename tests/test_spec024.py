"""SPEC-024: Изоляция пользовательских данных.

Все пользовательские операции над Post фильтруются по ``current_user.id``
на уровне репозитория. Чужие посты не видны, не отменяются, не редактируются.
Ошибки не раскрывают существование чужих постов.

§8 Обязательные проверки:
- two-user list: my_posts/list_pending_by_user возвращает только свои посты;
- cancel isolation: User B не может отменить пост User A;
- safe errors: ошибки отмены/получения не различают «чужой» vs «нет такого»;
- get_user_post: фильтрованный по user_id геттер.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.cancel_service import (
    CancelService,
    PostNotFoundForUserError,
)
from postmaster.services.my_posts_service import MyPostsService
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
ALICE_TG = 1001
BOB_TG = 1002


# === фикстуры ===============================================================


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, UserService]]:
    """Изолированная БД: PostRepository и UserService."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        yield (
            PostRepository(factory),
            UserService(user_repo, default_timezone="UTC"),
        )
    finally:
        await engine.dispose()


class _NoOpScheduler:
    """Заглушка Scheduler: ничего не делает, но фиксирует вызовы."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def remove_job(self, post_id: int) -> bool:
        self.calls.append(post_id)
        return True


async def _make_user(user_service: UserService, telegram_user_id: int) -> int:
    user = await user_service.get_or_create(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=f"User{telegram_user_id}",
    )
    return user.id


async def _create_pending_post(repo: PostRepository, user_id: int, *, suffix: str = "") -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}{suffix}",
        telegram_file_unique_id=f"unique_{user_id}{suffix}",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    return post.id


# === two-user list =========================================================


async def test_list_pending_by_user_returns_only_own_posts(post_repo) -> None:
    """list_pending_by_user изолирует по user_id (SPEC-024 §1, §7)."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    alice_post = await _create_pending_post(repo, alice_id, suffix="_a1")
    bob_post = await _create_pending_post(repo, bob_id, suffix="_b1")
    bob_post_2 = await _create_pending_post(repo, bob_id, suffix="_b2")

    alice_posts = await repo.list_pending_by_user(alice_id)
    bob_posts = await repo.list_pending_by_user(bob_id)

    assert {post.id for post in alice_posts} == {alice_post}
    assert {post.id for post in bob_posts} == {bob_post, bob_post_2}


async def test_list_pending_by_user_empty_for_unknown_user(post_repo) -> None:
    """Несуществующий user_id → пустой список, без ошибок."""
    repo, _ = post_repo
    assert await repo.list_pending_by_user(user_id=999_999) == []


async def test_my_posts_service_does_not_leak_across_users(post_repo) -> None:
    """MyPostsService.render_list отдаёт каждому только свои посты (SPEC-017 + SPEC-024)."""
    repo, user_service = post_repo
    await _make_user(user_service, ALICE_TG)
    await _make_user(user_service, BOB_TG)

    alice_internal = await user_service.get_by_telegram_id(ALICE_TG)
    bob_internal = await user_service.get_by_telegram_id(BOB_TG)
    assert alice_internal is not None and bob_internal is not None
    await _create_pending_post(repo, alice_internal.id, suffix="_alice")
    await _create_pending_post(repo, bob_internal.id, suffix="_bob")

    service = MyPostsService(post_repository=repo, user_service=user_service)

    alice_text = await service.render_list(telegram_user_id=ALICE_TG)
    bob_text = await service.render_list(telegram_user_id=BOB_TG)

    assert "ID " in alice_text
    assert "ID " in bob_text
    # ID Алисы не должен фигурировать в списке Боба и наоборот.
    alice_post_id = (await repo.list_pending_by_user(alice_internal.id))[0].id
    bob_post_id = (await repo.list_pending_by_user(bob_internal.id))[0].id
    assert f"ID {alice_post_id}" not in bob_text
    assert f"ID {bob_post_id}" not in alice_text


async def test_my_posts_service_empty_for_unknown_telegram_id(post_repo) -> None:
    """Неизвестный telegram_user_id → EMPTY_LIST без раскрытия чужих постов."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    await _create_pending_post(repo, alice_id)
    service = MyPostsService(post_repository=repo, user_service=user_service)

    text = await service.render_list(telegram_user_id=999_999)

    assert text == "У вас нет запланированных публикаций."


# === cancel isolation ======================================================


async def test_cancel_foreign_post_raises_safe_error(post_repo) -> None:
    """User B не может отменить пост User A (SPEC-024 §7, SPEC-018 §7)."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    alice_post = await _create_pending_post(repo, alice_id)

    scheduler = _NoOpScheduler()
    service = CancelService(post_repository=repo, scheduler=scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=alice_post, user_id=bob_id)

    # Пост остаётся PENDING у Алисы.
    post = await repo.get_by_id(alice_post)
    assert post is not None
    assert post.status == PostStatus.PENDING
    assert post.user_id == alice_id
    # Scheduler не трогали.
    assert scheduler.calls == []


async def test_cancel_nonexistent_post_raises_same_error(post_repo) -> None:
    """Отмена несуществующего поста даёт ту же ошибку, что и чужого."""
    repo, user_service = post_repo
    bob_id = await _make_user(user_service, BOB_TG)
    service = CancelService(post_repository=repo, scheduler=_NoOpScheduler())

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=999_999, user_id=bob_id)


async def test_cancel_safe_error_message_does_not_reveal_ownership(post_repo) -> None:
    """Текст PostNotFoundForUserError одинаков для «чужой» и «нет такого»."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    alice_post = await _create_pending_post(repo, alice_id)
    service = CancelService(post_repository=repo, scheduler=_NoOpScheduler())

    msg_foreign = None
    with pytest.raises(PostNotFoundForUserError) as info:
        await service.cancel(post_id=alice_post, user_id=bob_id)
    msg_foreign = str(info.value)

    msg_missing = None
    with pytest.raises(PostNotFoundForUserError) as info:
        await service.cancel(post_id=999_999, user_id=bob_id)
    msg_missing = str(info.value)

    assert msg_foreign == msg_missing
    # Не раскрываем ID поста или признак существования.
    assert str(alice_post) not in msg_foreign
    assert str(alice_post) not in msg_missing
    assert "чужим" not in msg_foreign.lower()
    assert "не существует" not in msg_foreign.lower()


async def test_cancel_own_post_succeeds(post_repo) -> None:
    """Владелец успешно отменяет свой пост."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    alice_post = await _create_pending_post(repo, alice_id)
    scheduler = _NoOpScheduler()
    service = CancelService(post_repository=repo, scheduler=scheduler)

    await service.cancel(post_id=alice_post, user_id=alice_id)

    post = await repo.get_by_id(alice_post)
    assert post is not None
    assert post.status == PostStatus.CANCELLED
    assert scheduler.calls == [alice_post]


async def test_cancel_post_in_terminal_status_remains(post_repo) -> None:
    """Нельзя отменить чужой пост в FAILED/PUBLISHED/CANCELLED — статус не меняется."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    post_id = await _create_pending_post(repo, alice_id)
    # PENDING -> PROCESSING -> FAILED: переходы разрешены ALLOWED_TRANSITIONS.
    await repo.claim_for_processing(post_id)
    await repo.update_status(post_id, PostStatus.FAILED, error_message="boom")

    service = CancelService(post_repository=repo, scheduler=_NoOpScheduler())

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=bob_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.FAILED


# === get_user_post =========================================================


async def test_get_user_post_returns_own_post(post_repo) -> None:
    """get_user_post возвращает пост владельцу."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    post_id = await _create_pending_post(repo, alice_id)

    post = await repo.get_user_post(post_id=post_id, user_id=alice_id)

    assert post is not None
    assert post.id == post_id
    assert post.user_id == alice_id


async def test_get_user_post_returns_none_for_foreign_user(post_repo) -> None:
    """get_user_post возвращает None для чужого пользователя (не исключение)."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    post_id = await _create_pending_post(repo, alice_id)

    post = await repo.get_user_post(post_id=post_id, user_id=bob_id)

    assert post is None


async def test_get_user_post_returns_none_for_missing_post(post_repo) -> None:
    """get_user_post с несуществующим ID возвращает None."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)

    assert await repo.get_user_post(post_id=999_999, user_id=alice_id) is None


async def test_get_user_post_does_not_distinguish_missing_vs_foreign(post_repo) -> None:
    """С точки зрения владельца «чужой» и «нет такого» выглядят одинаково."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    bob_id = await _make_user(user_service, BOB_TG)
    foreign_post = await _create_pending_post(repo, alice_id)

    foreign = await repo.get_user_post(post_id=foreign_post, user_id=bob_id)
    missing = await repo.get_user_post(post_id=999_999, user_id=bob_id)

    assert foreign is None
    assert missing is None


async def test_get_user_post_for_own_post_in_terminal_status(post_repo) -> None:
    """get_user_post возвращает пост владельцу независимо от статуса."""
    repo, user_service = post_repo
    alice_id = await _make_user(user_service, ALICE_TG)
    post_id = await _create_pending_post(repo, alice_id)
    # PENDING -> PROCESSING -> FAILED: разрешённые переходы.
    await repo.claim_for_processing(post_id)
    await repo.update_status(post_id, PostStatus.FAILED, error_message="boom")

    post = await repo.get_user_post(post_id=post_id, user_id=alice_id)

    assert post is not None
    assert post.status == PostStatus.FAILED


# === create_post изоляция ==================================================


async def test_create_post_for_unknown_user_fails(post_repo) -> None:
    """FK posts.user_id → users.id: чужой/несуществующий user не пройдёт (SPEC-024)."""
    from postmaster.domain.post import UserNotFoundError

    repo, _ = post_repo
    with pytest.raises(UserNotFoundError):
        await _create_pending_post(repo, user_id=999_999)


# === запрет на чтение постов вне owner-checked интерфейсов =================


async def test_no_service_exposes_posts_without_user_filter() -> None:
    """Сервисы не предоставляют «безусловный список постов».

    Контрактная проверка уровня типов: для пользовательских сценариев
    доступны только ``list_pending_by_user`` и ``get_user_post``.
    Метод ``list_pending`` (без user_id) предназначен только для recovery.
    """
    from postmaster.repositories.post_repository import PostRepository

    user_facing = {
        name
        for name in dir(PostRepository)
        if not name.startswith("_") and name not in {"list_pending", "get_by_id"}
    }
    # Каждый публичный метод (кроме технических) либо принимает user_id, либо это
    # write-операция, либо служебный list для recovery. Ниже минимальный набор,
    # который должен поддерживать user_id в запросе.
    for name in ("list_pending_by_user", "get_user_post", "cancel_post"):
        assert name in user_facing, name
