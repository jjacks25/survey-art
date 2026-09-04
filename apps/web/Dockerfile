# syntax=docker/dockerfile:1
# Local dev image for the React/Mantine SPA (Vite dev server with hot reload).
# In AWS the SPA is a static build served from S3/CloudFront (see `make deploy-web`),
# so this image is only used by docker-compose.
FROM node:20-slim
WORKDIR /app
COPY package.json ./
# No lockfile committed yet; install resolves from package.json.
RUN npm install
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]
