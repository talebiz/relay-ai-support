FROM node:20-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY package*.json ./
RUN npm install

COPY . .

EXPOSE 5173

# Run the Vite dev server, bound to 0.0.0.0 so it's reachable from
# outside the container (from the host machine's browser).
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
