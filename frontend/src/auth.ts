import NextAuth from "next-auth"
import Google from "next-auth/providers/google"
import { SignJWT } from "jose"

const JWT_SECRET = process.env.JWT_SECRET || "mock-jwt-secret-at-least-32-characters-long"

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
        return token
      }

      // Subsequent calls: check if access token is still valid
      // Refresh 5 minutes before actual expiry to avoid edge-case failures
      const expiresAt = (token.googleAccessTokenExpires as number) || 0
      if (Date.now() < expiresAt - 5 * 60 * 1000) {
        // Token is still fresh
        return token
      }

      // Token has expired (or is about to) — refresh it
      if (token.googleRefreshToken) {
        return await refreshGoogleAccessToken(token)
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
        
        // Generate standard signed HS256 JWT for our FastAPI backend
        const secretKey = new TextEncoder().encode(JWT_SECRET)
        const payload = {
          email: token.email,
          name: token.name,
          picture: token.picture,
          sub: token.sub,
          google_access_token: token.googleAccessToken,
          exp: Math.floor(Date.now() / 1000) + 24 * 60 * 60 // 1 day
        }
        
        session.apiToken = await new SignJWT(payload)
          .setProtectedHeader({ alg: "HS256" })
          .sign(secretKey)
      }
      return session
    }
  },
  secret: process.env.NEXTAUTH_SECRET || JWT_SECRET,
  trustHost: true,
  session: {
    strategy: "jwt"
  }
})
