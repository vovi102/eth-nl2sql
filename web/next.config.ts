import type { NextConfig } from "next";

// A static export: `npm run build` writes web/out, which `eth-nl2sql serve` serves next to
// the API, so no Node server runs at demo time. In `npm run dev`, set NEXT_PUBLIC_API_URL to
// the API (http://127.0.0.1:8000).
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
