"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { ModelInfo, Status } from "./types";

/** The models and the status of the API, the status polled (faster while a model loads). */
export function useServer() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [offline, setOffline] = useState(false);

  const refreshStatus = useCallback(async (): Promise<Status | null> => {
    try {
      const s = await api.status();
      setStatus(s);
      setOffline(false);
      return s;
    } catch {
      setOffline(true);
      return null;
    }
  }, []);

  const refreshModels = useCallback(async () => {
    try {
      const r = await api.models();
      setModels(r.models);
      setDefaultModel(r.default);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    refreshModels();
  }, [refreshModels]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const loop = async () => {
      const s = await refreshStatus();
      if (stopped) return;
      const fast = !s || s.warming || s.llama?.state === "loading" || s.busy;
      timer = setTimeout(loop, fast ? 1500 : 5000);
    };
    loop();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [refreshStatus]);

  // models come before the API is up: fetch them again once it answers
  useEffect(() => {
    if (!offline && models.length === 0 && status) refreshModels();
  }, [offline, models.length, status, refreshModels]);

  const byId = useMemo(() => Object.fromEntries(models.map((m) => [m.id, m])), [models]);
  return { models, byId, defaultModel, status, offline, refreshStatus };
}
