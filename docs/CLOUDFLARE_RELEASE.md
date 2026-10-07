# Cloudflare production release checkpoint

No command in this document should be run until the release checkpoint is explicitly approved.

## 1. Rotate and purge

Revoke every real value that has ever appeared in `.env`, including the admin password, `JWT_SECRET`, `ENCRYPTION_KEY`, provider credentials, and API keys. Rotation is required even after the Git purge.

The repository also contained a tracked credential file named `ADMIN_EMAIL=admin@coldcalls.md` and a legacy `coldcalls.db`; purge both paths as well. Use `git filter-repo --path .env --path 'ADMIN_EMAIL=admin@coldcalls.md' --path coldcalls.db --invert-paths --force`, inspect all local branches and tags, and verify all three paths plus the rewritten tree with a secrets scanner. Only then update the public repository with `git push --force-with-lease --all` and `git push --force-with-lease --tags`. Every collaborator must re-clone after the rewrite.

## 2. Recreate D1

Export the current remote database before deletion. Recreate the `coldcalls` database, replace `database_id` in `wrangler.jsonc`, apply `0001_init.sql`, and run the readback:

```sh
npx wrangler d1 export coldcalls --remote --output ../coldcalls-pre-recreate.sql
npm run db:migrate:remote
ADMIN_EMAIL='new-admin@example.com' node scripts/bootstrap_d1.mjs
```

The bootstrap asks for the new admin password without echo and reads back the admin and both plans.

## 3. Secrets and first manual deploy

Create fresh values and store only runtime secrets with Wrangler:

- `JWT_SECRET`
- `ENCRYPTION_KEY` (a valid 32-byte URL-safe Fernet key)
- `ETHERSCAN_API_KEY`

`USDT_WALLET_ADDRESS` and other non-secret configuration remain in `vars`. Run the first deploy manually, copy the returned `https://coldcalls.<account-subdomain>.workers.dev` URL into `BASE_URL`, regenerate types, and deploy once more. Then validate `/health`, login, dashboard, campaigns, and `/css/custom.css`.

## 4. Workers Builds

Connect the public GitHub repository, production branch `main`, and disable preview deployments.

- Build command: `npm run check`
- Deploy command: `npm run deploy:production`
- Root directory: repository root

The deploy command applies D1 migrations, lists and reads back the migrated state, and only then runs `wrangler deploy --keep-vars`.

Local tests and HTTP smoke tests prove the application code, bindings, assets, and deployment path. They do not prove real phone calls, provider acceptance, DTMF, telephone media, settlement, or realtime voice quality.
