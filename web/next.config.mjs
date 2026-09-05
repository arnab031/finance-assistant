/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Off so SSE is not gzipped. `text/event-stream` matches text/*, so Next's
  // default compressor would pool the 24-char token frames and release them in
  // bursts. Cloudflare compresses at the edge anyway.
  compress: false,

  // One public origin: the browser talks to the Next server, which forwards
  // /api/* to FastAPI. Lets a single tunnel cover both without CORS.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
    ];
  },
};
export default nextConfig;
