import type { UserVoximplantCredentialRow, VoximplantCredentials } from './user_voximplant_service';
import { signRs256Jwt } from './jwt_sign';

export type VoximplantApiResponse = Record<string, any>;

export function VoximplantManagementService(credentials: VoximplantCredentials) {
  const baseUrl = 'https://api.voximplant.com/platform_api';

  async function buildJwt(): Promise<string> {
    const now = Math.floor(Date.now() / 1000);
    return signRs256Jwt(
      credentials.private_key,
      {
        iss: credentials.service_account_email,
        sub: credentials.service_account_email,
        aud: 'https://api.voximplant.com',
        iat: now,
        exp: now + 900,
      },
      { kid: credentials.key_id },
    );
  }

  async function request(
    methodName: string,
    payload?: Record<string, unknown>,
  ): Promise<VoximplantApiResponse> {
    const token = await buildJwt();
    const endpoint = `${baseUrl}/${methodName}`;
    const body = new URLSearchParams();
    body.set('account_id', credentials.account_id);
    if (payload) {
      for (const [field, value] of Object.entries(payload)) body.set(field, String(value));
    }
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body,
      signal: AbortSignal.timeout(30000),
    });
    if (!response.ok) {
      const kind = response.status >= 400 && response.status < 500 ? 'Client' : 'Server';
      throw new Error(
        `${kind} error '${response.status} ${response.statusText}' for url '${endpoint}'`,
      );
    }
    const data = (await response.json()) as VoximplantApiResponse;
    const result = data && typeof data === 'object' && 'result' in data ? data.result : 1;
    // GetAccountInfo-style verbs return result as an object on success; only
    // scalar false / error strings mean failure.
    if (result === false || (typeof result === 'string' && !['1', 'true', 'True'].includes(result))) {
      throw new Error(`Voximplant ${methodName} failed: ${JSON.stringify(data)}`);
    }
    return data;
  }

  async function validateCredentials(): Promise<VoximplantApiResponse> {
    return request('GetAccountInfo');
  }

  async function addApplication(applicationName: string): Promise<VoximplantApiResponse> {
    return request('AddApplication', { application_name: applicationName });
  }

  async function addScenario(scenarioName: string, script: string): Promise<VoximplantApiResponse> {
    return request('AddScenario', { scenario_name: scenarioName, script });
  }

  async function setScenario(scenarioId: number, script: string): Promise<VoximplantApiResponse> {
    return request('SetScenarioInfo', { scenario_id: scenarioId, script });
  }

  async function addRule(
    ruleName: string,
    scenarioId: number,
    applicationId: string,
  ): Promise<VoximplantApiResponse> {
    return request('AddRule', {
      rule_name: ruleName,
      rule_pattern: ruleName,
      scenario_id: scenarioId,
      application_id: applicationId,
    });
  }

  async function setRule(
    ruleId: number,
    scenarioId: number,
    applicationId: string,
    ruleName: string,
  ): Promise<VoximplantApiResponse> {
    return request('SetRuleInfo', {
      rule_id: ruleId,
      rule_name: ruleName,
      rule_pattern: ruleName,
      scenario_id: scenarioId,
      application_id: applicationId,
    });
  }

  async function addCallerId(callerId: string): Promise<VoximplantApiResponse> {
    return request('AddCallerID', { callerid: callerId });
  }

  async function verifyCallerId(voxCallerIdId: number): Promise<VoximplantApiResponse> {
    return request('VerifyCallerID', { callerid_id: voxCallerIdId });
  }

  async function activateCallerId(
    voxCallerIdId: number,
    verificationCode: string,
  ): Promise<VoximplantApiResponse> {
    return request('ActivateCallerID', {
      callerid_id: voxCallerIdId,
      verification_code: verificationCode,
    });
  }

  async function startScenario(
    ruleId: number,
    scriptCustomData: Record<string, unknown>,
  ): Promise<VoximplantApiResponse> {
    return request('StartScenarios', {
      rule_id: ruleId,
      script_custom_data: JSON.stringify(scriptCustomData),
    });
  }

  return {
    validateCredentials,
    addApplication,
    addScenario,
    setScenario,
    addRule,
    setRule,
    addCallerId,
    verifyCallerId,
    activateCallerId,
    startScenario,
  };
}

export type VoximplantManagementServiceInstance = ReturnType<typeof VoximplantManagementService>;

export function buildVoximplantScenario(user: { id: number }): string {
  return `
require(Modules.Net);

let outboundCall = null;
let transferCall = null;
let data = JSON.parse(VoxEngine.customData() || "{}");
let transferStarted = false;
let pressOneSatisfied = !data.press_1_to_talk_with_agent;
let gatherTimer = null;

function postStatus(status, extra) {
  let payload = {
    token: data.callback_token,
    call_sid: outboundCall ? outboundCall.id() : null,
    status: status,
    duration: extra && extra.duration ? extra.duration : 0,
    answered_by: extra && extra.answered_by ? extra.answered_by : null,
    error_message: extra && extra.error_message ? extra.error_message : null
  };
  Net.httpRequestAsync(data.status_callback_url, {
    method: "POST",
    headers: ["Content-Type: application/json"],
    postData: JSON.stringify(payload)
  });
}

function finish(status, extra) {
  postStatus(status, extra || {});
  VoxEngine.terminate();
}

function connectTransfer() {
  if (transferStarted) return;
  transferStarted = true;
  transferCall = VoxEngine.callPSTN(data.transfer_number, data.caller_id);
  transferCall.addEventListener(CallEvents.Connected, function() {
    VoxEngine.sendMediaBetween(outboundCall, transferCall);
    VoxEngine.sendMediaBetween(transferCall, outboundCall);
    postStatus("in_progress", {});
  });
  transferCall.addEventListener(CallEvents.Disconnected, function() {
    finish("completed", {});
  });
  transferCall.addEventListener(CallEvents.Failed, function(e) {
    finish("failed", { error_message: e.reason || "transfer_failed" });
  });
}

function promptForAgent() {
  outboundCall.say("Press 1 to talk with an agent.");
  outboundCall.handleTones(true);
  gatherTimer = setTimeout(function() {
    if (!pressOneSatisfied) {
      finish("no_answer", { answered_by: "dtmf_timeout" });
    }
  }, 8000);
}

VoxEngine.addEventListener(AppEvents.Started, function() {
  outboundCall = VoxEngine.callPSTN(data.to_number, data.caller_id);
  postStatus("queued", {});

  outboundCall.addEventListener(CallEvents.Connected, function() {
    postStatus("ringing", {});
    if (data.audio_url) {
      outboundCall.startPlayback(data.audio_url);
      return;
    }
    if (data.press_1_to_talk_with_agent) {
      promptForAgent();
      return;
    }
    connectTransfer();
  });

  outboundCall.addEventListener(CallEvents.PlaybackFinished, function() {
    if (data.press_1_to_talk_with_agent) {
      promptForAgent();
      return;
    }
    connectTransfer();
  });

  outboundCall.addEventListener(CallEvents.ToneReceived, function(e) {
    if (e.tone === "1" && !pressOneSatisfied) {
      pressOneSatisfied = true;
      if (gatherTimer) clearTimeout(gatherTimer);
      connectTransfer();
    }
  });

  outboundCall.addEventListener(CallEvents.Disconnected, function(e) {
    finish("completed", { duration: e.duration || 0 });
  });

  outboundCall.addEventListener(CallEvents.Failed, function(e) {
    finish("failed", { error_message: e.reason || "call_failed" });
  });
});
`.trim();
}

export async function ensureUserVoximplantResources(
  db: D1Database,
  user: { id: number },
  credentialsRow: UserVoximplantCredentialRow,
  credentials: VoximplantCredentials,
): Promise<UserVoximplantCredentialRow> {
  const service = VoximplantManagementService(credentials);
  await service.validateCredentials();

  const scenarioName = `coldcalls-user-${user.id}-scenario`;
  const ruleName = `coldcalls-user-${user.id}-rule`;
  const appName = credentials.application_id.trim() || `coldcalls-user-${user.id}`;
  const scenarioScript = buildVoximplantScenario(user);

  let voxAppId = credentialsRow.vox_app_id;
  let voxScenarioId = credentialsRow.vox_scenario_id;
  let voxRuleId = credentialsRow.vox_rule_id;

  if (!voxAppId) {
    if (/^\d+$/.test(appName)) {
      voxAppId = Number(appName);
    } else {
      const response = await service.addApplication(appName);
      voxAppId = Number(response.application_id || response.app_id || 0) || null;
    }
  }

  if (!voxScenarioId) {
    const response = await service.addScenario(scenarioName, scenarioScript);
    voxScenarioId = Number(response.scenario_id || 0) || null;
  } else {
    await service.setScenario(voxScenarioId, scenarioScript);
  }

  const applicationId = String(voxAppId ?? appName);

  if (!voxRuleId) {
    const response = await service.addRule(ruleName, voxScenarioId ?? 0, applicationId);
    voxRuleId = Number(response.rule_id || 0) || null;
  } else {
    await service.setRule(voxRuleId, voxScenarioId ?? 0, applicationId, ruleName);
  }

  const now = new Date().toISOString();
  await db
    .prepare(
      'UPDATE user_voximplant_credentials SET vox_app_id = ?, vox_scenario_id = ?, vox_rule_id = ?, provision_status = ?, provision_error = ?, provisioned_at = ?, updated_at = ? WHERE id = ?',
    )
    .bind(voxAppId, voxScenarioId, voxRuleId, 'configured', null, now, now, credentialsRow.id)
    .run();

  return {
    ...credentialsRow,
    vox_app_id: voxAppId,
    vox_scenario_id: voxScenarioId,
    vox_rule_id: voxRuleId,
    provision_status: 'configured',
    provision_error: null,
    provisioned_at: now,
    updated_at: now,
  };
}

export interface CallerIdLike {
  vox_callerid_id: number | null;
  phone_number: string;
}

export async function ensureVoximplantCallerId(
  callerId: CallerIdLike,
  credentials: VoximplantCredentials,
): Promise<VoximplantApiResponse> {
  const service = VoximplantManagementService(credentials);
  if (callerId.vox_callerid_id) return { callerid_id: callerId.vox_callerid_id };
  return service.addCallerId(callerId.phone_number);
}
