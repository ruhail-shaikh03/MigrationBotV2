import NextAuth from "next-auth"
import Google from "next-auth/providers/google"
import { SignJWT } from "jose"

const JWT_SECRET = process.env.JWT_SECRET || "mock-jwt-secret-at-least-32-characters-long"

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
      if (account) {
        token.googleAccessToken = account.access_token
        if (account.refresh_token) {
          token.googleRefreshToken = account.refresh_token
        }
      }
      return token
    },
    async session({ session, token }: any) {
      if (token) {
        session.googleAccessToken = token.googleAccessToken
        session.googleRefreshToken = token.googleRefreshToken
        session.user.id = token.sub
        
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
