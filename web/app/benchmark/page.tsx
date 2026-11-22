import type { Metadata } from "next";
import { BenchmarkApp } from "@/components/BenchmarkApp";

export const metadata: Metadata = { title: "Benchmark · eth-nl2sql" };

export default function Page() {
  return <BenchmarkApp />;
}
