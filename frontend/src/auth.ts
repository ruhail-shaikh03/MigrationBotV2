import NextAuth from "next-auth"
import Google from "next-auth/providers/google"
import type { JWT } from "next-auth/jwt"
import { SignJWT } from "jose"

/**
 * Resolve the backend JWT signing secret, at request time.
 *
 * Still no fallback: a shared, publicly-known default here is exactly what let a
 * deployment that forgot to set this accept tokens forged against that known string
 * (see backend/app/config.py's JWT_SECRET, which is required for the same reason).
 *
 * What changed is *when* the check runs. This used to be a module-scope `const` +
 * `throw`, which fired during `next build`'s "Collecting page data" step: Next.js
 * imports every route handler to build the route manifest, and
 * app/api/auth/[...nextauth]/route.ts re-exports `handlers` from this module — so a
 * Docker builder stage with no env vars failed the build outright. Validating inside
 * the callback keeps importing this module side-effect-free while still failing loudly
 * on the first real session request if the secret is genuinely missing.
 */
function getJwtSecret(): string {
  const secret = process.env.JWT_SECRET
  if (!secret) {
    throw new Error("JWT_SECRET environment variable is required and has no default.")
  }
  return secret
}

/**
 * Refresh the Google access token using the stored refresh token.
 * Returns the updated token fields, or marks the token with an error on failure.
 */
async function refreshGoogleAccessToken(token: any) {
  try {
    const response = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: process.env.GOOGLE_CLIENT_ID || "",
        client_secret: process.env.GOOGLE_CLIENT_SECRET || "",
        grant_type: "refresh_token",
        refresh_token: token.googleRefreshToken,
      }),
    })

    const refreshed = await response.json()

    if (!response.ok) {
      console.error("Google token refresh failed:", refreshed)
      return { ...token, error: "RefreshAccessTokenError" }
    }

    return {
      ...token,
      googleAccessToken: refreshed.access_token,
      // Google returns expires_in in seconds; convert to absolute ms timestamp
      googleAccessTokenExpires: Date.now() + refreshed.expires_in * 1000,
      // Google only issues a new refresh_token when scopes change; keep existing
      googleRefreshToken: refreshed.refresh_token ?? token.googleRefreshToken,
    }
  } catch (error) {
    console.error("Error refreshing Google access token:", error)
    return { ...token, error: "RefreshAccessTokenError" }
  }
}

const API_TOKEN_TTL_SECONDS = 24 * 60 * 60
// Re-mint an hour before expiry so a client holding the previous value always has a
// usable token, rather than one that dies between two session fetches.
const API_TOKEN_RENEW_BEFORE_MS = 60 * 60 * 1000

/**
 * Mint the backend JWT, or reuse the cached one, and cache it on the NextAuth token.
 *
 * This used to be minted inline in the `session` callback on *every* invocation, with
 * `exp` computed from `Date.now()` — so each call produced a different `exp`, hence a
 * different signature, hence a different string. `session.apiToken` therefore changed
 * identity on every session fetch, and SessionProvider refetches on window focus. On
 * the client that value is a dependency of the chat WebSocket (useWebSocket), so simply
 * alt-tabbing back to the tab tore down and rebuilt the connection — and since
 * `message_history` lives per-connection in chat.py:websocket_chat_endpoint, it silently
 * discarded the conversation context.
 *
 * Caching has to happen here in the `jwt` callback rather than in `session`: only the
 * value returned from `jwt` is re-encoded into the session cookie, so a field set in
 * `session` would not survive to the next request.
 */
async function withApiToken(token: JWT): Promise<JWT> {
  const cachedExpires = (token.apiTokenExpires as number) || 0
  const stillFresh = Date.now() < cachedExpires - API_TOKEN_RENEW_BEFORE_MS
  // googleAccessTokenExpires is rewritten every time refreshGoogleAccessToken() succeeds,
  // so it doubles as a compact generation marker for "the embedded Google token changed".
  // Comparing that number avoids stashing a third copy of the access token in the cookie.
  const embedsCurrentGoogleToken = token.apiTokenIssuedFor === token.googleAccessTokenExpires

  if (token.apiToken && stillFresh && embedsCurrentGoogleToken) {
    return token
  }

  const expSeconds = Math.floor(Date.now() / 1000) + API_TOKEN_TTL_SECONDS
  const secretKey = new TextEncoder().encode(getJwtSecret())

  token.apiToken = await new SignJWT({
    email: token.email,
    name: token.name,
    picture: token.picture,
    sub: token.sub,
    google_access_token: token.googleAccessToken,
    // Lets WS-path Sheets clients self-refresh instead of dying ~1h into a
    // long-lived socket when this JWT (valid 24h) outlives the Google token it
    // carries. Only the admin REST path had this before, via a separate header.
    google_refresh_token: token.googleRefreshToken,
    exp: expSeconds
  })
    .setProtectedHeader({ alg: "HS256" })
    .sign(secretKey)

  token.apiTokenExpires = expSeconds * 1000
  token.apiTokenIssuedFor = token.googleAccessTokenExpires

  return token
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  providers: [
    Google({
      clientId: process.env.GOOGLE_CLIENT_ID,
      clientSecret: process.env.GOOGLE_CLIENT_SECRET,
      authorization: {
        params: {
          scope: "openid email profile https://www.googleapis.com/auth/spreadsheets",
          prompt: "consent",
          access_type: "offline",
          response_type: "code"
        }
      }
    })
  ],
  callbacks: {
    async jwt({ token, account }) {
      // First sign-in: capture all Google tokens and expiry
      if (account) {
        token.googleAccessToken = account.access_token
        // expires_at from Google is in seconds; convert to ms
        token.googleAccessTokenExpires = (account.expires_at ?? 0) * 1000
        if (account.refresh_token) {
          token.googleRefreshToken = account.refresh_token
        }
        return await withApiToken(token)
      }

      // Subsequent calls: check if access token is still valid
      // Refresh 5 minutes before actual expiry to avoid edge-case failures
      const expiresAt = (token.googleAccessTokenExpires as number) || 0
      if (Date.now() < expiresAt - 5 * 60 * 1000) {
        // Token is still fresh — withApiToken reuses the cached backend JWT here, which
        // is what keeps session.apiToken byte-identical across session fetches.
        return await withApiToken(token)
      }

      // Token has expired (or is about to) — refresh it. The refreshed token carries a
      // new googleAccessTokenExpires, so withApiToken re-mints to embed the new Google
      // access token rather than handing the backend a dead one.
      if (token.googleRefreshToken) {
        return await withApiToken(await refreshGoogleAccessToken(token))
      }

      // No refresh token available — can't recover
      return { ...token, error: "NoRefreshToken" }
    },
    async session({ session, token }: any) {
      if (token) {
        session.googleAccessToken = token.googleAccessToken
        session.googleRefreshToken = token.googleRefreshToken
        session.user.id = token.sub
        session.error = token.error // Surface token errors to the client

        // Hand back the JWT minted and cached by withApiToken() in the `jwt` callback.
        // Deliberately not minted here: this callback runs on every session fetch, so
        // signing per call is what made the value change identity each time.
        session.apiToken = token.apiToken
      }
      return session
    }
  },
  // Read straight from the environment rather than via getJwtSecret(): this object
  // literal is evaluated at module scope, so calling the validating getter here would
  // reintroduce the exact build-time throw this refactor removes. Passing `undefined`
  // is safe — NextAuth's setEnvDefaults() only assigns defaults at import
  // (next-auth/lib/env.js), and the MissingSecret check lives in assertConfig(), which
  // runs per-request inside Auth() (@auth/core/index.js). A genuinely unset secret in
  // production therefore still fails, at the first auth request rather than at import.
  secret: process.env.NEXTAUTH_SECRET || process.env.JWT_SECRET,
  trustHost: true,
  session: {
    strategy: "jwt"
  }
})
