import { Suspense } from "react";
import { ChatApp } from "@/components/ChatApp";

export default function Page() {
  // useSearchParams (?q=, ?model= from the benchmark page) needs a Suspense boundary in a static export
  return (
    <Suspense>
      <ChatApp />
    </Suspense>
  );
}
