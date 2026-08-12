/**
 * Module augmentation for the fields `auth.ts` puts on the NextAuth session and JWT.
 *
 * Without this, `Session` and `JWT` are just NextAuth's defaults, so every call site
 * had to launder the extra fields through `(session as any).apiToken` — eleven casts
 * across six files, none of them checked. A typo in `apiToken` compiled cleanly and
 * failed at runtime as a missing Authorization header.
 *
 * These names must stay in step with what `auth.ts` actually assigns in the `jwt` and
 * `session` callbacks; that file is the source of truth, this only describes it.
 */
import type { DefaultSession } from "next-auth"

declare module "next-auth" {
  interface Session {
    /** HS256 JWT minted by `withApiToken`, sent as the bearer to FastAPI (TDD §4.1). */
    apiToken?: string
    /** Forwarded to Sheets calls the backend makes on the user's behalf. */
    googleAccessToken?: string
    googleRefreshToken?: string
    /** Set when the refresh flow could not recover, e.g. "NoRefreshToken". */
    error?: string
    user: {
      id?: string
    } & DefaultSession["user"]
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    apiToken?: string
    /** Epoch ms; `withApiToken` re-mints near expiry rather than on every read. */
    apiTokenExpires?: number
    /** The `googleAccessTokenExpires` the cached apiToken was minted against. */
    apiTokenIssuedFor?: number
    googleAccessToken?: string
    googleRefreshToken?: string
    googleAccessTokenExpires?: number
    error?: string
  }
}
