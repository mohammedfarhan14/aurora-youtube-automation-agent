# Encryption Keyring and Rotation Runbook

Aurora Studio encrypts email-outbox message bodies and YouTube refresh tokens with AES-256-GCM. Each persisted record carries a positive key version. Email and YouTube use separate key domains and must never share key material.

## Configuration

New settings:

| Domain | Keyring JSON | Active write version | Legacy setting |
|---|---|---|---|
| Email outbox | `EMAIL_OUTBOX_ENCRYPTION_KEYRING` | `EMAIL_OUTBOX_ACTIVE_KEY_VERSION` | `EMAIL_OUTBOX_ENCRYPTION_KEY` |
| YouTube tokens | `INTEGRATION_TOKEN_ENCRYPTION_KEYRING` | `INTEGRATION_TOKEN_ACTIVE_KEY_VERSION` | `INTEGRATION_TOKEN_ENCRYPTION_KEY` |

A keyring is a JSON object mapping canonical positive integer versions to 64-character hexadecimal AES-256 keys:

```dotenv
EMAIL_OUTBOX_ENCRYPTION_KEYRING={"1":"<old-64-hex-key>","2":"<new-64-hex-key>"}
EMAIL_OUTBOX_ACTIVE_KEY_VERSION=2

INTEGRATION_TOKEN_ENCRYPTION_KEYRING={"1":"<old-64-hex-key>","2":"<new-64-hex-key>"}
INTEGRATION_TOKEN_ACTIVE_KEY_VERSION=2
```

Do not copy the placeholders. Generate each domain's key independently using a cryptographically secure random generator, for example:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

The old single-key environment variables remain supported as version 1 for backward compatibility. If a legacy variable and a keyring both define version 1, their values must match. Once the keyring is established, remove the legacy variable to avoid duplicate configuration. Production requires an email-outbox keyring (or legacy version-1 key); YouTube's keyring is required when YouTube OAuth is enabled.

The active version must exist in the keyring. All versions referenced by retained database rows must remain configured for decryption.

## Safe rotation procedure

A rotation is an operational change. It is not performed automatically at application startup.

1. **Back up first.** Take and verify a restorable database backup. Securely preserve the old encryption keys alongside the backup recovery material. A database backup without its corresponding encryption keys is not a usable recovery point.
2. **Deploy keyring-capable code everywhere.** Upgrade API and worker processes while retaining version 1 as the active key. Confirm every instance can read version-1 records. Do not start rotation while old application versions are still running.
3. **Add the new key without removing the old key.** Configure both versions in the keyring and set the active version to the new version on every API and worker instance. Confirm all instances have restarted and are using the same keyring/active version. Pause or drain writers during this configuration transition if the hosting platform cannot guarantee a coordinated rollout.
4. **Run one rotation operator at a time.** From `backend/api`, run the domain-specific command:
   ```bash
   python -m app.services.encryption_key_rotation --domain email --batch-size 100 --max-batches 100
   python -m app.services.encryption_key_rotation --domain youtube --batch-size 100 --max-batches 100
   ```
   Run only the domains configured in the environment. Each batch is one database transaction, locks selected rows with `FOR UPDATE SKIP LOCKED`, and updates ciphertext and key version atomically. No plaintext or key material is logged.
5. **Resume safely if interrupted.** Rerun the same command with the same keyring and active version. Rows already at the active version are skipped. If a batch cannot decrypt an old record, that transaction fails without overwriting the record; investigate the missing/mismatched key rather than deleting or resetting ciphertext.
6. **Verify completion.** The command checks for records still using non-active versions and fails if any remain. Resolve transient row-lock contention and rerun until the verification passes. Also inspect the rotation output and application/worker health.
7. **Retire old keys only after the recovery window.** Do not remove an old key until no live records require it and all database backups that may contain old-version ciphertext have expired or have separately retained recovery keys. Keep the old key in a restricted offline/secret-manager recovery archive for the required backup-retention period.

For a large dataset, increase the number of invocations rather than the transaction batch size. The maximum batch size is 1,000 records and the default maximum is 100 transactions per invocation. Repeated invocations are safe.

## Concurrency and rollback properties

- New email payloads and YouTube refresh tokens are encrypted using the configured active version.
- Decryption selects the key by the version persisted with the record and authenticates the ciphertext using AES-GCM.
- The rotator uses bounded transactions and row locks. It never deletes records or clears credentials.
- A failed decrypt aborts the current batch. A committed batch is complete; an uncommitted batch rolls back. Rerunning processes only rows that still need rotation.
- The email worker may continue processing during rotation because old keys remain available. Schedule rotation during a low-traffic period and avoid overlapping rotation operators.
- YouTube connect/disconnect operations lock the channel row so they serialize with token rotation.
- Keep old keys configured until verification and backup-retention requirements are satisfied.

## Recovery and compromise

If the active key is lost, do not generate a replacement and overwrite configuration as a workaround. Restore the exact key from the approved secret backup or restore a database backup whose ciphertext can be decrypted with the retained keyring. If a key is suspected compromised, treat this as an incident: restrict access, preserve evidence, assess affected ciphertext/backups, plan a controlled re-encryption, and revoke/re-authorize affected external credentials when required. Never place key values in Git, issue comments, CI logs, screenshots, or support tickets.
