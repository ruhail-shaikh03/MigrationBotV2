# Stage 1: Install dependencies
FROM node:20-alpine AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# Stage 2: Rebuild the source code
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1

# Defence in depth, not the actual fix — src/auth.ts validates JWT_SECRET lazily now,
# so this build no longer depends on the variable being set. This keeps a *future*
# module-scope env read (here or anywhere else in a route's import chain) from failing
# "Collecting page data" with a non-obvious error, since Next.js imports every route
# handler at build time to assemble the route manifest.
#
# This is a throwaway build-stage value and never reaches the shipped image: it lives
# only in the builder stage, while the runtime secret is injected into the runner
# stage by docker-compose.yml (frontend service, `environment: JWT_SECRET`). Never
# pass a real secret via ARG — build args are recorded in the image history.
ARG JWT_SECRET=build-placeholder
ENV JWT_SECRET=$JWT_SECRET

RUN npm run build

# Stage 3: Runner
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public

# Set the correct permission for prerender cache
RUN mkdir .next
RUN chown nextjs:nodejs .next

# Automatically leverage output traces to reduce image size
# https://nextjs.org/docs/advanced-features/output-file-tracing
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
