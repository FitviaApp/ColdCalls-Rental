"""
Voximplant Management API integration.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from jose import jwt
from sqlalchemy.orm import Session

from app.models import CallerID, User, UserVoximplantCredential
from app.services.user_voximplant_service import VoximplantCredentials

logger = logging.getLogger(__name__)


class VoximplantManagementService:
    def __init__(self, credentials: VoximplantCredentials):
        self.credentials = credentials
        self.base_url = "https://api.voximplant.com/platform_api"

    def _build_jwt(self) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "iss": self.credentials.service_account_email,
            "sub": self.credentials.service_account_email,
            "aud": "https://api.voximplant.com",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
        }
        headers = {"kid": self.credentials.key_id}
        return jwt.encode(payload, self.credentials.private_key, algorithm="RS256", headers=headers)

    def _request(self, method_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._build_jwt()
        endpoint = f"{self.base_url}/{method_name}"
        body = {"account_id": self.credentials.account_id}
        if payload:
            body.update(payload)

        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                endpoint,
                data=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and str(data.get("result", 1)) not in {"1", "true", "True"}:
            raise RuntimeError(f"Voximplant {method_name} failed: {data}")
        return data

    def validate_credentials(self) -> dict[str, Any]:
        return self._request("GetAccountInfo")

    def add_application(self, application_name: str) -> dict[str, Any]:
        return self._request("AddApplication", {"application_name": application_name})

    def add_scenario(self, scenario_name: str, script: str) -> dict[str, Any]:
        return self._request(
            "AddScenario",
            {
                "scenario_name": scenario_name,
                "script": script,
            },
        )

    def set_scenario(self, scenario_id: int, script: str) -> dict[str, Any]:
        return self._request(
            "SetScenarioInfo",
            {
                "scenario_id": scenario_id,
                "script": script,
            },
        )

    def add_rule(self, rule_name: str, scenario_id: int, application_id: str) -> dict[str, Any]:
        return self._request(
            "AddRule",
            {
                "rule_name": rule_name,
                "rule_pattern": rule_name,
                "scenario_id": scenario_id,
                "application_id": application_id,
            },
        )

    def set_rule(self, rule_id: int, scenario_id: int, application_id: str, rule_name: str) -> dict[str, Any]:
        return self._request(
            "SetRuleInfo",
            {
                "rule_id": rule_id,
                "rule_name": rule_name,
                "rule_pattern": rule_name,
                "scenario_id": scenario_id,
                "application_id": application_id,
            },
        )

    def add_caller_id(self, caller_id: str) -> dict[str, Any]:
        return self._request("AddCallerID", {"callerid": caller_id})

    def verify_caller_id(self, vox_callerid_id: int) -> dict[str, Any]:
        return self._request("VerifyCallerID", {"callerid_id": vox_callerid_id})

    def activate_caller_id(self, vox_callerid_id: int, verification_code: str) -> dict[str, Any]:
        return self._request(
            "ActivateCallerID",
            {
                "callerid_id": vox_callerid_id,
                "verification_code": verification_code,
            },
        )

    def start_scenario(self, rule_id: int, script_custom_data: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "StartScenarios",
            {
                "rule_id": rule_id,
                "script_custom_data": json.dumps(script_custom_data),
            },
        )


def build_voximplant_scenario(user: User) -> str:
    del user
    return f"""
require(Modules.Net);

let outboundCall = null;
let transferCall = null;
let data = JSON.parse(VoxEngine.customData() || "{{}}");
let transferStarted = false;
let pressOneSatisfied = !data.press_1_to_talk_with_agent;
let gatherTimer = null;

function postStatus(status, extra) {{
  let payload = {{
    token: data.callback_token,
    call_sid: outboundCall ? outboundCall.id() : null,
    status: status,
    duration: extra && extra.duration ? extra.duration : 0,
    answered_by: extra && extra.answered_by ? extra.answered_by : null,
    error_message: extra && extra.error_message ? extra.error_message : null
  }};
  Net.httpRequestAsync(data.status_callback_url, {{
    method: "POST",
    headers: ["Content-Type: application/json"],
    postData: JSON.stringify(payload)
  }});
}}

function finish(status, extra) {{
  postStatus(status, extra || {{}});
  VoxEngine.terminate();
}}

function connectTransfer() {{
  if (transferStarted) return;
  transferStarted = true;
  transferCall = VoxEngine.callPSTN(data.transfer_number, data.caller_id);
  transferCall.addEventListener(CallEvents.Connected, function() {{
    VoxEngine.sendMediaBetween(outboundCall, transferCall);
    VoxEngine.sendMediaBetween(transferCall, outboundCall);
    postStatus("in_progress", {{}});
  }});
  transferCall.addEventListener(CallEvents.Disconnected, function() {{
    finish("completed", {{}});
  }});
  transferCall.addEventListener(CallEvents.Failed, function(e) {{
    finish("failed", {{ error_message: e.reason || "transfer_failed" }});
  }});
}}

function promptForAgent() {{
  outboundCall.say("Press 1 to talk with an agent.");
  outboundCall.handleTones(true);
  gatherTimer = setTimeout(function() {{
    if (!pressOneSatisfied) {{
      finish("no_answer", {{ answered_by: "dtmf_timeout" }});
    }}
  }}, 8000);
}}

VoxEngine.addEventListener(AppEvents.Started, function() {{
  outboundCall = VoxEngine.callPSTN(data.to_number, data.caller_id);
  postStatus("queued", {{}});

  outboundCall.addEventListener(CallEvents.Connected, function() {{
    postStatus("ringing", {{}});
    if (data.audio_url) {{
      outboundCall.startPlayback(data.audio_url);
      return;
    }}
    if (data.press_1_to_talk_with_agent) {{
      promptForAgent();
      return;
    }}
    connectTransfer();
  }});

  outboundCall.addEventListener(CallEvents.PlaybackFinished, function() {{
    if (data.press_1_to_talk_with_agent) {{
      promptForAgent();
      return;
    }}
    connectTransfer();
  }});

  outboundCall.addEventListener(CallEvents.ToneReceived, function(e) {{
    if (e.tone === "1" && !pressOneSatisfied) {{
      pressOneSatisfied = true;
      if (gatherTimer) clearTimeout(gatherTimer);
      connectTransfer();
    }}
  }});

  outboundCall.addEventListener(CallEvents.Disconnected, function(e) {{
    finish("completed", {{ duration: e.duration || 0 }});
  }});

  outboundCall.addEventListener(CallEvents.Failed, function(e) {{
    finish("failed", {{ error_message: e.reason || "call_failed" }});
  }});
}});
""".strip()


def ensure_user_voximplant_resources(
    db: Session,
    user: User,
    credentials_row: UserVoximplantCredential,
    credentials: VoximplantCredentials,
) -> UserVoximplantCredential:
    service = VoximplantManagementService(credentials)
    service.validate_credentials()

    scenario_name = f"coldcalls-user-{user.id}-scenario"
    rule_name = f"coldcalls-user-{user.id}-rule"
    app_name = credentials.application_id.strip() or f"coldcalls-user-{user.id}"
    scenario_script = build_voximplant_scenario(user)

    if not credentials_row.vox_app_id:
        if app_name.isdigit():
            credentials_row.vox_app_id = int(app_name)
        else:
            response = service.add_application(app_name)
            credentials_row.vox_app_id = int(
                response.get("application_id")
                or response.get("app_id")
                or 0
            ) or None

    if not credentials_row.vox_scenario_id:
        response = service.add_scenario(scenario_name, scenario_script)
        credentials_row.vox_scenario_id = int(response.get("scenario_id") or 0) or None
    else:
        service.set_scenario(credentials_row.vox_scenario_id, scenario_script)

    if not credentials_row.vox_rule_id:
        response = service.add_rule(
            rule_name,
            credentials_row.vox_scenario_id,
            str(credentials_row.vox_app_id or app_name),
        )
        credentials_row.vox_rule_id = int(response.get("rule_id") or 0) or None
    else:
        service.set_rule(
            credentials_row.vox_rule_id,
            credentials_row.vox_scenario_id,
            str(credentials_row.vox_app_id or app_name),
            rule_name,
        )

    credentials_row.provision_status = "configured"
    credentials_row.provision_error = None
    credentials_row.provisioned_at = datetime.utcnow()
    db.flush()
    return credentials_row


def ensure_voximplant_caller_id(
    caller_id: CallerID,
    credentials: VoximplantCredentials,
) -> dict[str, Any]:
    service = VoximplantManagementService(credentials)
    if caller_id.vox_callerid_id:
        return {"callerid_id": caller_id.vox_callerid_id}
    response = service.add_caller_id(caller_id.phone_number)
    return response
