import { describe, expect, it } from 'vitest';
import { decryptFernet, encryptFernet, hashPassword, verifyPassword } from '../src/auth';

const PYTHON_FERNET_KEY = 'VhJcoDZyNIQTAZDUvw2c3hlIQ2HQF8NgcArzr90XvQU=';
const PYTHON_FERNET_TOKEN = 'gAAAAABqxSVc6Nc5stmAh8FiQqVvJsUHPD8_Ji3Y8CrXCSP0Ck3F2gPR9X-gXswVwR57kCxbph8tJGJDMcjcOXK7oW4G9w8ofp040GKtcBOysDrgMWBvr_w=';

describe('authentication compatibility', () => {
  it('decrypts a fixture emitted by cryptography.Fernet', () => {
    expect(decryptFernet(PYTHON_FERNET_KEY, PYTHON_FERNET_TOKEN)).toBe('provider-secret-fixture');
  });

  it('round-trips a Fernet credential and preserves the standard token version', () => {
    const token = encryptFernet(PYTHON_FERNET_KEY, 'new-provider-secret');
    expect(decryptFernet(PYTHON_FERNET_KEY, token)).toBe('new-provider-secret');
    expect(Buffer.from(token, 'base64url')[0]).toBe(0x80);
  });

  it('keeps bcrypt password hashes compatible', () => {
    const hash = hashPassword('correct horse battery staple');
    expect(verifyPassword('correct horse battery staple', hash)).toBe(true);
    expect(verifyPassword('wrong', hash)).toBe(false);
  });
});
