// ============================================================
// src/main/store/vault.ts — Secret storage (keytar + mock fallback)
// ============================================================

// ── keytar with mock fallback ────────────────────────────────

interface KeytarLike {
  setPassword(service: string, account: string, password: string): Promise<void>;
  getPassword(service: string, account: string): Promise<string | null>;
  deletePassword(service: string, account: string): Promise<boolean>;
  findCredentials(service: string): Promise<Array<{ account: string; password: string }>>;
}

let keytar: KeytarLike | null = null;
let useMock = process.env.MOCK_VAULT === 'true';

if (!useMock) {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require('keytar');
    keytar = (mod.default ?? mod) as KeytarLike;
  } catch {
    useMock = true;
  }
}

// ── Constants ────────────────────────────────────────────────

const SERVICE_NAME = 'tongtian-office';

// ── In-memory mock store ─────────────────────────────────────

const mockStore = new Map<string, string>();

const mockKeytar: KeytarLike = {
  async setPassword(_service: string, account: string, password: string): Promise<void> {
    mockStore.set(account, password);
  },
  async getPassword(_service: string, account: string): Promise<string | null> {
    return mockStore.get(account) ?? null;
  },
  async deletePassword(_service: string, account: string): Promise<boolean> {
    return mockStore.delete(account);
  },
  async findCredentials(_service: string): Promise<Array<{ account: string; password: string }>> {
    const result: Array<{ account: string; password: string }> = [];
    for (const [account, password] of mockStore.entries()) {
      result.push({ account, password });
    }
    return result;
  },
};

// ── Runtime memory cache ─────────────────────────────────────

const memoryCache = new Map<string, string>();

// ── Vault class ──────────────────────────────────────────────

class Vault {
  private static instance: Vault | null = null;
  private keytarImpl: KeytarLike;

  private constructor() {
    this.keytarImpl = keytar ?? mockKeytar;
  }

  static getInstance(): Vault {
    if (!Vault.instance) {
      Vault.instance = new Vault();
    }
    return Vault.instance;
  }

  /**
   * Store an API key for a given model config ID.
   */
  async storeApiKey(id: string, key: string): Promise<void> {
    await this.keytarImpl.setPassword(SERVICE_NAME, id, key);
    memoryCache.set(id, key);
  }

  /**
   * Retrieve an API key by model config ID.
   * Checks memory cache first, then falls back to keytar/mock.
   */
  async getApiKey(id: string): Promise<string | null> {
    const cached = memoryCache.get(id);
    if (cached !== undefined) {
      return cached;
    }

    const password = await this.keytarImpl.getPassword(SERVICE_NAME, id);
    if (password) {
      memoryCache.set(id, password);
    }
    return password;
  }

  /**
   * Delete an API key by model config ID.
   */
  async deleteApiKey(id: string): Promise<boolean> {
    memoryCache.delete(id);
    return this.keytarImpl.deletePassword(SERVICE_NAME, id);
  }

  /**
   * Clear all stored API keys.
   */
  async clearAll(): Promise<void> {
    const credentials = await this.keytarImpl.findCredentials(SERVICE_NAME);
    for (const cred of credentials) {
      await this.keytarImpl.deletePassword(SERVICE_NAME, cred.account);
    }
    memoryCache.clear();
  }

  /**
   * Clear the in-memory cache (called on process exit).
   */
  clearMemoryCache(): void {
    memoryCache.clear();
  }
}

// Clear memory cache on process exit
const clearOnExit = (): void => {
  memoryCache.clear();
};

process.on('exit', clearOnExit);
process.on('SIGINT', () => { clearOnExit(); process.exit(0); });
process.on('SIGTERM', () => { clearOnExit(); process.exit(0); });

export default Vault;
