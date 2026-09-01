// ============================================================
// src/main/util/crypto.ts — AES-256-GCM encrypt/decrypt
// ============================================================

import crypto from 'crypto';

// ── Types ────────────────────────────────────────────────────

export interface EncryptedPayload {
  encrypted: string;  // Base64 encoded ciphertext
  iv: string;         // Base64 encoded initialization vector
  salt: string;       // Base64 encoded salt
  tag: string;        // Base64 encoded auth tag
}

// ── Constants ────────────────────────────────────────────────

const ALGORITHM = 'aes-256-gcm';
const IV_LENGTH = 16;       // 128 bits
const SALT_LENGTH = 32;     // 256 bits
const KEY_LENGTH = 32;      // 256 bits for AES-256
const TAG_LENGTH = 16;      // 128 bits
const SCRYPT_COST = 16384;  // N parameter for scrypt (2^14)
const SCRYPT_BLOCK_SIZE = 8;
const SCRYPT_PARALLELIZATION = 1;

// ── Key Derivation ───────────────────────────────────────────

/**
 * Derive a 256-bit key from a password and salt using scrypt.
 */
export function deriveKey(password: string, salt: Buffer): Buffer {
  return crypto.scryptSync(
    password,
    salt,
    KEY_LENGTH,
    {
      N: SCRYPT_COST,
      r: SCRYPT_BLOCK_SIZE,
      p: SCRYPT_PARALLELIZATION,
    }
  );
}

// ── Encrypt ──────────────────────────────────────────────────

/**
 * Encrypt a plaintext string using AES-256-GCM.
 * Key is derived from masterKey using scrypt with a random salt.
 *
 * @param plaintext - The string to encrypt
 * @param masterKey - The master key / passphrase
 * @returns EncryptedPayload with all components Base64 encoded
 */
export function encrypt(plaintext: string, masterKey: string): EncryptedPayload {
  const salt = crypto.randomBytes(SALT_LENGTH);
  const iv = crypto.randomBytes(IV_LENGTH);
  const key = deriveKey(masterKey, salt);

  const cipher = crypto.createCipheriv(ALGORITHM, key, iv);

  const encrypted = Buffer.concat([
    cipher.update(plaintext, 'utf8'),
    cipher.final(),
  ]);

  const tag = cipher.getAuthTag();

  return {
    encrypted: encrypted.toString('base64'),
    iv: iv.toString('base64'),
    salt: salt.toString('base64'),
    tag: tag.toString('base64'),
  };
}

// ── Decrypt ──────────────────────────────────────────────────

/**
 * Decrypt an EncryptedPayload using AES-256-GCM.
 *
 * @param payload - The encrypted payload with Base64 encoded components
 * @param masterKey - The master key / passphrase used during encryption
 * @returns The decrypted plaintext string
 * @throws Error if authentication fails (wrong key or tampered data)
 */
export function decrypt(payload: EncryptedPayload, masterKey: string): string {
  const salt = Buffer.from(payload.salt, 'base64');
  const iv = Buffer.from(payload.iv, 'base64');
  const tag = Buffer.from(payload.tag, 'base64');
  const encrypted = Buffer.from(payload.encrypted, 'base64');

  const key = deriveKey(masterKey, salt);

  const decipher = crypto.createDecipheriv(ALGORITHM, key, iv);
  decipher.setAuthTag(tag);

  const decrypted = Buffer.concat([
    decipher.update(encrypted),
    decipher.final(),
  ]);

  return decrypted.toString('utf8');
}
