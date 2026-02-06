# Step 1: Build the Angular app
FROM node:22-alpine AS build-step
WORKDIR /app

# Copy package files first
COPY package*.json ./
RUN npm install

# Copy everything and build
COPY . .
RUN npm run build

# Step 2: Serve the STATIC files using Nginx
FROM nginx:alpine
# In Angular 21, the static files go into dist/[project-name]/browser
COPY --from=build-step /app/dist/front/browser /usr/share/nginx/html

# Copy a custom nginx config if you have one, or just use default
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]