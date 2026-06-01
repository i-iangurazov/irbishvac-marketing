from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from ..clients.claude import ClaudeClient
from ..clients.notion import NotionApiError, NotionClient, last_edited_or_now
from ..clients.slack import SlackClient, slack_mention
from ..config import Settings
from ..models import STATUS_CHANGE_POST_STATUSES, Campaign, Task
from ..persistence import Persistence
from .campaign_health import CampaignHealthService
from .formatting import status_update_text, task_status_blocks
from .owner_mapping import OwnerResolver
from .reminders import REMINDER_TYPE_1H, reminder_decision, reminder_message


logger = logging.getLogger(__name__)


class TaskProcessor:
    def __init__(
        self,
        settings: Settings,
        db: Persistence,
        notion: NotionClient,
        slack: SlackClient,
        claude: ClaudeClient,
        owner_resolver: OwnerResolver,
        campaign_health: CampaignHealthService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.notion = notion
        self.slack = slack
        self.claude = claude
        self.owner_resolver = owner_resolver
        self.campaign_health = campaign_health

    def poll_once(self) -> int:
        if not self.notion.available or not self.settings.notion_tasks_database_id:
            logger.warning(
                "notion_poll_skipped_missing_config",
                extra={
                    "missing_api_key": not self.notion.available,
                    "missing_tasks_database_id": not bool(self.settings.notion_tasks_database_id),
                },
            )
            return 0
        run_id = self.db.log_run_start("notion_poll")
        try:
            if self.db.get_kv("notion_tasks_baseline_initialized") != "true" or self.db.count_task_states() == 0:
                tasks = self.notion.query_all_tasks()
                self.process_tasks(tasks, emit_transitions=False)
                reminders_sent = self._process_deadline_reminders_safely(tasks)
                max_last_edited = max((last_edited_or_now(task) for task in tasks), default=None)
                if max_last_edited:
                    self.db.set_kv("notion_tasks_last_processed", max_last_edited)
                self.db.set_kv("notion_tasks_baseline_initialized", "true")
                self.db.log_run_complete(run_id, "baseline_initialized", {"tasks_seen": len(tasks), "reminders_sent": reminders_sent})
                logger.info("notion_poll_baseline_initialized", extra={"tasks_seen": len(tasks), "reminders_sent": reminders_sent})
                return 0
            since = self._poll_since_with_overlap(self.db.get_kv("notion_tasks_last_processed"))
            tasks = self.notion.query_tasks_modified_since(since)
            processed = self.process_tasks(tasks)
            reminders_sent = self._process_deadline_reminders_safely()
            max_last_edited = max((last_edited_or_now(task) for task in tasks), default=since)
            if max_last_edited:
                self.db.set_kv("notion_tasks_last_processed", max_last_edited)
            self.db.log_run_complete(run_id, "completed", {"tasks_seen": len(tasks), "transitions": processed, "reminders_sent": reminders_sent})
            logger.info("notion_poll_completed", extra={"tasks_seen": len(tasks), "transitions": processed, "reminders_sent": reminders_sent})
            return processed
        except NotionApiError as exc:
            self.db.log_run_complete(
                run_id,
                "skipped",
                {"status": exc.status, "code": exc.code, "notion_message": exc.message},
            )
            logger.warning(
                "notion_poll_skipped_api_error",
                extra={"status": exc.status, "code": exc.code, "notion_message": exc.message},
            )
            return 0
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("notion_poll_failed")
            raise

    def process_tasks(self, tasks: list[Task], emit_transitions: bool = True) -> int:
        transitions = 0
        for task in tasks:
            self._ensure_original_deadline(task)
            previous = self.db.get_task_state(task.id)
            if not previous:
                self._save_state(task)
                continue
            previous_status = str(previous.get("status") or "")
            previous_deadline = previous.get("deadline")
            if emit_transitions and previous_status != task.status and task.status in STATUS_CHANGE_POST_STATUSES:
                if self.process_status_change(task, previous_status, previous_deadline):
                    transitions += 1
                    self._save_state(task)
                continue
            self._save_state(task)
        return transitions

    def _process_deadline_reminders_safely(self, tasks: list[Task] | None = None) -> int:
        try:
            reminder_tasks = tasks if tasks is not None else self.notion.query_all_tasks()
            return self.process_deadline_reminders(reminder_tasks)
        except Exception as exc:
            logger.warning("task_reminder_scan_failed", exc_info=True, extra={"error": str(exc)})
            return 0

    def process_deadline_reminders(self, tasks: list[Task], now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        sent_count = 0
        for task in tasks:
            decision = reminder_decision(
                task,
                now,
                self.settings.timezone,
                minutes_before=self.settings.task_reminder_minutes_before,
                date_only_deadline_hour=self.settings.task_date_only_deadline_hour,
            )
            if not decision.eligible:
                continue
            if not decision.reminder_key or not decision.deadline_at:
                continue
            if self.db.has_task_reminder(decision.reminder_key):
                logger.info("duplicate_task_reminder_suppressed", extra={"task_id": task.id, "reminder_type": REMINDER_TYPE_1H})
                continue
            slack_user_id = self._resolve_task_slack_user(task)
            if not slack_user_id:
                logger.warning(
                    "task_reminder_skipped_unmapped_owner",
                    extra={"task_id": task.id, "owner": task.owner_name, "owner_email": task.owner_email},
                )
                continue
            sent_at = datetime.now(timezone.utc)
            ts = self.slack.dm_user(slack_user_id, reminder_message(task))
            if not ts:
                logger.warning("task_reminder_slack_failed", extra={"task_id": task.id, "slack_user_id": slack_user_id})
                continue
            if not self.db.record_task_reminder_sent(
                reminder_key=decision.reminder_key,
                task_id=task.id,
                reminder_type=REMINDER_TYPE_1H,
                owner_notion_user_id=task.owner_notion_user_id,
                slack_user_id=slack_user_id,
                deadline_at=decision.deadline_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                sent_at=sent_at.replace(microsecond=0).isoformat(),
            ):
                logger.info("duplicate_task_reminder_suppressed", extra={"task_id": task.id, "reminder_type": REMINDER_TYPE_1H})
                continue
            self.notion.set_last_reminder_sent_at(task.id, sent_at)
            sent_count += 1
            logger.info(
                "task_reminder_sent",
                extra={"task_id": task.id, "owner": task.owner_name, "slack_user_id": slack_user_id, "deadline_at": decision.deadline_at.isoformat()},
            )
        return sent_count

    def _resolve_task_slack_user(self, task: Task) -> str | None:
        owner_email = self.owner_resolver.resolve_owner_email(task.owner)
        if owner_email:
            slack_user_id = self.slack.lookup_user_by_email(owner_email)
            if slack_user_id:
                self.db.upsert_owner_mapping(owner_email, task.owner_name, slack_user_id, owner_email)
                return slack_user_id
            logger.warning(
                "task_reminder_email_lookup_failed",
                extra={"task_id": task.id, "owner": task.owner_name, "owner_email": owner_email},
            )
        return self.owner_resolver.resolve_slack_user(task.owner)

    def process_pending_transitions(self) -> int:
        tasks = self.notion.query_all_tasks()
        processed = self.process_tasks(tasks, emit_transitions=True)
        max_last_edited = max((last_edited_or_now(task) for task in tasks), default=None)
        if max_last_edited:
            self.db.set_kv("notion_tasks_last_processed", max_last_edited)
        self.db.set_kv("notion_tasks_baseline_initialized", "true")
        logger.info("notion_pending_transitions_processed", extra={"tasks_seen": len(tasks), "transitions": processed})
        return processed

    def rebuild_baseline(self) -> int:
        self.db.clear_task_state()
        tasks = self.notion.query_all_tasks()
        self.process_tasks(tasks, emit_transitions=False)
        max_last_edited = max((last_edited_or_now(task) for task in tasks), default=None)
        if max_last_edited:
            self.db.set_kv("notion_tasks_last_processed", max_last_edited)
        self.db.set_kv("notion_tasks_baseline_initialized", "true")
        logger.info("notion_task_baseline_rebuilt", extra={"tasks_seen": len(tasks)})
        return len(tasks)

    def _poll_since_with_overlap(self, since: str | None) -> str | None:
        if not since:
            return None
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return since
        return (parsed - timedelta(seconds=self.settings.poll_overlap_seconds)).isoformat()

    def process_status_change(self, task: Task, previous_status: str | None, previous_deadline: str | None = None) -> bool:
        dedupe_key = f"task-status:{task.id}:{task.status}:{last_edited_or_now(task)}"
        if self.db.has_status_transition(dedupe_key):
            logger.info("duplicate_status_transition_suppressed", extra={"task_id": task.id, "status": task.status})
            return True
        text = status_update_text(task)
        ts = self.slack.post_message(
            self.settings.slack_marketing_ops_channel_id,
            text,
            blocks=task_status_blocks(task),
        )
        if not ts:
            logger.warning("status_transition_pending_slack_post_failed", extra={"task_id": task.id, "status": task.status})
            return False
        if not self.db.record_status_transition(
            task_id=task.id,
            from_status=previous_status,
            to_status=task.status,
            notion_last_edited_time=last_edited_or_now(task),
            dedupe_key=dedupe_key,
        ):
            logger.info("duplicate_status_transition_suppressed", extra={"task_id": task.id, "status": task.status})
            return True
        self.db.attach_slack_thread_to_transition(dedupe_key, self.settings.slack_marketing_ops_channel_id, ts)
        self.db.save_slack_thread(task.id, task.status, self.settings.slack_marketing_ops_channel_id, ts, dedupe_key)
        if task.status == "Completed":
            self._verify_completed(task, ts)
        elif task.status == "Delayed":
            self._verify_delayed(task, previous_deadline, ts)
        elif task.status == "Blocked":
            self._verify_blocked(task, ts)
        elif task.status == "Canceled":
            logger.info("task_canceled_processed", extra={"task_id": task.id, "task": task.name})
        return True

    def repost_missing_slack_updates(self) -> int:
        missing = self.db.get_transitions_missing_slack()
        if not missing:
            return 0
        tasks_by_id = {task.id: task for task in self.notion.query_all_tasks()}
        posted = 0
        for transition in missing:
            task = tasks_by_id.get(str(transition["task_id"]))
            if not task:
                logger.warning("missing_slack_repost_task_not_found", extra={"task_id": transition["task_id"]})
                continue
            ts = self.slack.post_message(
                self.settings.slack_marketing_ops_channel_id,
                status_update_text(task),
                blocks=task_status_blocks(task),
            )
            if not ts:
                logger.warning("missing_slack_repost_failed", extra={"task_id": task.id, "status": task.status})
                continue
            dedupe_key = str(transition["dedupe_key"])
            self.db.attach_slack_thread_to_transition(dedupe_key, self.settings.slack_marketing_ops_channel_id, ts)
            self.db.save_slack_thread(task.id, task.status, self.settings.slack_marketing_ops_channel_id, ts, dedupe_key)
            posted += 1
        logger.info("missing_slack_updates_reposted", extra={"posted_count": posted})
        return posted

    def _verify_completed(self, task: Task, thread_ts: str | None) -> None:
        issues: list[str] = []
        if not task.deliverable_link and not task.notes_issues:
            issues.append("please attach the deliverable / link / proof.")
        if task.deadline and date.today() > task.deadline + timedelta(days=1):
            logger.info("task_completed_after_deadline", extra={"task_id": task.id, "deadline": task.deadline.isoformat()})
        if task.child_task_ids or task.dependency_task_ids:
            open_related = self._open_related_tasks(task.child_task_ids + task.dependency_task_ids)
            if open_related:
                issues.append("related child tasks or dependencies are still open: " + ", ".join(open_related))
        if not issues:
            logger.info("completed_task_verified_clean", extra={"task_id": task.id})
            return
        flag_count = self.db.increment_verification_flag_count(task.id)
        comment = self.claude.draft_verification_comment("Completed", issues)
        self._comment_and_flag(task, comment, mention_owner=True)
        self._reply_thread(thread_ts, comment)
        logger.warning("verification_flag_created", extra={"task_id": task.id, "status": task.status, "flag_count": flag_count})
        if flag_count >= 2 or self._task_campaign_over_budget(task):
            self._dm_tim(f"Completed task needs verification: {task.name}\n{comment}")

    def _verify_delayed(self, task: Task, previous_deadline: str | None, thread_ts: str | None) -> None:
        delay_count = self.db.increment_delay_count(task.id)
        issues: list[str] = []
        current_deadline = task.deadline.isoformat() if task.deadline else None
        if current_deadline == previous_deadline:
            issues.append("please update the deadline when marking a task delayed.")
        if not task.notes_issues:
            issues.append("please add the reason in Notes / Issues.")
        if issues:
            comment = self.claude.draft_verification_comment("Delayed", issues)
            self._comment_and_flag(task, comment, mention_owner=True)
            self._reply_thread(thread_ts, comment)
            logger.warning("verification_flag_created", extra={"task_id": task.id, "status": task.status, "flag_count": delay_count})
        if delay_count >= 2:
            self._dm_tim(f"Task delayed twice: {task.name}\nDeadline: {task.deadline_iso}\nReason: {task.notes_issues or 'missing'}")

    def _verify_blocked(self, task: Task, thread_ts: str | None) -> None:
        if not task.needs_from_others:
            comment = self.claude.draft_verification_comment("Blocked", ["please fill in Needs From Others with the person or team needed."])
            self._comment_and_flag(task, comment, mention_owner=True)
            self._reply_thread(thread_ts, comment)
            logger.warning("verification_flag_created", extra={"task_id": task.id, "status": task.status})
            return
        mentions = [slack_mention(user_id) for user_id in self.owner_resolver.mentions_for_text(task.needs_from_others)]
        suffix = " " + " ".join(mentions) if mentions else ""
        message = f"Blocked task needs input: {task.name}\nNeeds from: {task.needs_from_others}{suffix}\n{task.url}"
        self.slack.post_message(self.settings.slack_marketing_ops_channel_id, message, thread_ts=thread_ts)

    def _comment_and_flag(self, task: Task, comment: str, mention_owner: bool) -> None:
        try:
            self.notion.add_task_comment(task, comment, mention_owner=mention_owner)
        except Exception as exc:
            logger.warning("notion_comment_failed", extra={"task_id": task.id, "error": str(exc)})
        self.notion.set_needs_verification(task.id, True)

    def _reply_thread(self, thread_ts: str | None, text: str) -> None:
        if not thread_ts:
            return
        self.slack.reply(self.settings.slack_marketing_ops_channel_id, thread_ts, text)

    def _dm_tim(self, text: str) -> None:
        if not self.settings.slack_tim_user_id:
            logger.warning("tim_dm_skipped_missing_user", extra={"text": text[:200]})
            return
        self.slack.dm_user(self.settings.slack_tim_user_id, text)
        logger.info("tim_escalation_sent", extra={"subject": "task_verification"})

    def _task_campaign_over_budget(self, task: Task) -> bool:
        if not task.linked_campaign_ids:
            return False
        try:
            campaigns = self.notion.query_all_campaigns()
        except Exception:
            logger.warning("campaign_budget_check_failed", exc_info=True, extra={"task_id": task.id})
            return False
        campaigns_by_id = {campaign.id: campaign for campaign in campaigns}
        return any(
            self.campaign_health.is_budget_overrun(campaigns_by_id[campaign_id])
            for campaign_id in task.linked_campaign_ids
            if campaign_id in campaigns_by_id
        )

    def _open_related_tasks(self, task_ids: list[str]) -> list[str]:
        if not task_ids:
            return []
        try:
            all_tasks = self.notion.query_all_tasks()
        except Exception:
            logger.warning("related_task_check_failed", exc_info=True, extra={"task_ids": task_ids})
            return []
        by_id = {task.id: task for task in all_tasks}
        return [by_id[task_id].name for task_id in task_ids if task_id in by_id and by_id[task_id].is_open()]

    def _ensure_original_deadline(self, task: Task) -> None:
        if task.original_deadline or not task.deadline:
            return
        self.notion.set_original_deadline_if_missing(task)

    def _save_state(self, task: Task) -> None:
        self.db.upsert_task_state(
            task_id=task.id,
            name=task.name,
            owner_name=task.owner_name,
            owner_notion_user_id=task.owner_notion_user_id,
            owner_email=task.owner_email,
            status=task.status,
            deadline=task.deadline.isoformat() if task.deadline else None,
            original_deadline=task.original_deadline.isoformat() if task.original_deadline else None,
            last_edited_time=last_edited_or_now(task),
        )
