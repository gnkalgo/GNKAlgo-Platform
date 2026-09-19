FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
FROM node:24-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_URL=/api/v1
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build
FROM node:24-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app
RUN addgroup -S gnk && adduser -S gnk -G gnk
COPY --from=build --chown=gnk:gnk /app/.next/standalone ./
COPY --from=build --chown=gnk:gnk /app/.next/static ./.next/static
COPY --from=build --chown=gnk:gnk /app/public ./public
USER gnk
EXPOSE 3000
CMD ["node", "server.js"]
