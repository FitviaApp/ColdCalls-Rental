import { bindings, defineConfig, exports } from "cf/config";

export default defineConfig({
	worker: {
		name: "coldcalls",
		compatibilityDate: "2026-10-06",
		compatibilityFlags: [
			"nodejs_compat",
		],
		entrypoint: "src/index.ts",
		workersDev: true,
		previewUrls: false,
		observability: {
			enabled: true,
			headSamplingRate: 1,
		},
		env: {
			BASE_URL: bindings.text("https://coldcalls.mralnilam.workers.dev"),
			APP_NAME: bindings.text("ColdCalls Platform"),
			MAX_USERS: bindings.text("4"),
			OPENAI_API_BASE: bindings.text("https://api.openai.com/v1"),
			OPENAI_REALTIME_URL: bindings.text("wss://api.openai.com/v1/realtime"),
			OPENAI_REALTIME_MODEL: bindings.text("gpt-realtime-2.1"),
			REALTIME_SESSION_MAX_SECONDS: bindings.text("7200"),
			ELEVENLABS_API_BASE: bindings.text("https://api.elevenlabs.io/v1"),
			ELEVENLABS_TTS_MODEL_ID: bindings.text("eleven_flash_v2_5"),
			ELEVENLABS_OUTPUT_FORMAT: bindings.text("ulaw_8000"),
			USDT_WALLET_ADDRESS: bindings.text(""),
			USDT_CONTRACT: bindings.text("0xdAC17F958D2ee523a2206206994597C13D831ec7"),
			DB: bindings.d1({
				name: "coldcalls",
			}),
			AUDIO_R2: bindings.r2({
				name: "coldcalls-audios",
			}),
			CAMPAIGN_COORDINATORS: bindings.durableObject({
				worker: "coldcalls",
				exportName: "CampaignCoordinator",
			}),
			REALTIME_CALLS: bindings.durableObject({
				worker: "coldcalls",
				exportName: "RealtimeCallSession",
			}),
			ASSETS: bindings.assets(),
		},
		exports: {
			CampaignCoordinator: exports.durableObject({
				storage: "sqlite",
			}),
			RealtimeCallSession: exports.durableObject({
				storage: "sqlite",
			}),
		},
	},
});
