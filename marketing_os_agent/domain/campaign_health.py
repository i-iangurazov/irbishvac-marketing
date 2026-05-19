from __future__ import annotations

import logging
from datetime import date

from ..clients.slack import SlackClient
from ..config import Settings
from ..models import Campaign, Task
from ..persistence import Persistence


logger = logging.getLogger(__name__)


class CampaignHealthService:
    def __init__(self, settings: Settings, db: Persistence, slack: SlackClient) -> None:
        self.settings = settings
        self.db = db
        self.slack = slack

    def scan(self, campaigns: list[Campaign], tasks: list[Task], today: date) -> list[str]:
        tasks_by_id = {task.id: task for task in tasks}
        alerts: list[str] = []
        for campaign in campaigns:
            for alert in self.check_campaign(campaign, tasks_by_id, today):
                alerts.append(alert)
        return alerts

    def check_campaign(self, campaign: Campaign, tasks_by_id: dict[str, Task], today: date) -> list[str]:
        alerts: list[str] = []
        budget_alert = self._budget_alert(campaign)
        if budget_alert and self.db.mark_campaign_flag(campaign.id, "budget_overrun", {"message": budget_alert}):
            alerts.append(budget_alert)
            self._dm_tim("Budget overrun flagged", budget_alert)
            logger.warning("campaign_budget_overrun_flagged", extra={"campaign_id": campaign.id, "campaign": campaign.name})
        risk_alert = self._progress_risk_alert(campaign, tasks_by_id, today)
        if risk_alert and self.db.mark_campaign_flag(campaign.id, "progress_risk", {"message": risk_alert}):
            alerts.append(risk_alert)
            self._dm_tim("Campaign progress risk flagged", risk_alert)
            logger.warning("campaign_progress_risk_flagged", extra={"campaign_id": campaign.id, "campaign": campaign.name})
        return alerts

    def is_budget_overrun(self, campaign: Campaign) -> bool:
        return self._budget_alert(campaign) is not None

    def _budget_alert(self, campaign: Campaign) -> str | None:
        if campaign.planned_spend is None or campaign.actual_spend is None:
            return None
        threshold_amount = campaign.planned_spend * (1 + self.settings.budget_overrun_threshold_percent / 100)
        if campaign.actual_spend > campaign.planned_spend or campaign.actual_spend >= threshold_amount:
            return (
                f"{campaign.name}: actual spend ${campaign.actual_spend:,.2f} "
                f"exceeds planned spend ${campaign.planned_spend:,.2f} "
                f"(threshold {self.settings.budget_overrun_threshold_percent:.1f}%)."
            )
        return None

    def _progress_risk_alert(self, campaign: Campaign, tasks_by_id: dict[str, Task], today: date) -> str | None:
        if not campaign.start_date or not campaign.end_date:
            return None
        total_days = max((campaign.end_date - campaign.start_date).days, 1)
        elapsed_days = (today - campaign.start_date).days
        window_percent = (elapsed_days / total_days) * 100
        if window_percent <= self.settings.campaign_risk_window_percent:
            return None
        linked_tasks = [tasks_by_id[task_id] for task_id in campaign.linked_task_ids if task_id in tasks_by_id]
        if not linked_tasks and campaign.linked_task_ids:
            completed_percent = 0.0
        elif not linked_tasks:
            return None
        else:
            completed = sum(1 for task in linked_tasks if task.status == "Completed")
            completed_percent = (completed / len(linked_tasks)) * 100
        if completed_percent < self.settings.campaign_risk_task_completion_percent:
            return (
                f"{campaign.name}: campaign is {window_percent:.1f}% through its window "
                f"with {completed_percent:.1f}% of linked tasks completed."
            )
        return None

    def _dm_tim(self, subject: str, message: str) -> None:
        if not self.settings.slack_tim_user_id:
            logger.warning("tim_dm_skipped_missing_user", extra={"subject": subject, "alert_message": message})
            return
        self.slack.dm_user(self.settings.slack_tim_user_id, f"{subject}\n{message}")
        logger.info("tim_escalation_sent", extra={"subject": subject})
