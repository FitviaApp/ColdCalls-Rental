import { defineWranglerConfig } from "wrangler/experimental-config";

export default defineWranglerConfig({
	build: {
		command: "node scripts/precompile_templates.mjs",
	},
	types: {
		generate: false,
	},
	assetsDirectory: "./app/static",
});
