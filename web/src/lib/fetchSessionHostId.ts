import type { QueryClient } from "@tanstack/react-query";
import { getSessionSlim } from "./sessionsApi";
import {
  getSessionParent,
  sessionRoutingHostId,
  setSessionHost,
  setSessionParent,
} from "./sessionHost";
import type { Session } from "./types";

// Match the cold host-routing budget in useSession.ts.
const MAX_HOST_CHAIN_READS = 16;

interface HostResolution {
  hostId: string | null;
  snapshots: Session[];
}

const inFlight = new Map<string, Promise<HostResolution>>();

/** Resolve the nearest own host binding with fresh metadata for each ancestry hop.
 * Concurrent callers share a walk; completed host bindings are never cached. */
export async function fetchSessionHostId(
  sessionId: string,
  { queryClient }: { queryClient?: QueryClient } = {},
): Promise<string | null> {
  let pending = inFlight.get(sessionId);
  if (!pending) {
    pending = fetchSessionHostChain(sessionId);
    inFlight.set(sessionId, pending);
  }
  try {
    const { hostId, snapshots } = await pending;
    for (const source of snapshots) queryClient?.setQueryData(["session", source.id], source);
    return hostId;
  } finally {
    if (inFlight.get(sessionId) === pending) inFlight.delete(sessionId);
  }
}

async function fetchSessionHostChain(sessionId: string): Promise<HostResolution> {
  const snapshots: Session[] = [];
  try {
    const visited = new Set<string>();
    let id: string | null = sessionId;
    while (id !== null && !visited.has(id) && visited.size < MAX_HOST_CHAIN_READS) {
      visited.add(id);
      const hopId: string = id;
      // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
      const source: Session = await getSessionSlim(hopId);
      snapshots.push(source);
      setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
      // Side chats link to their source through labels rather than parentSessionId.
      setSessionParent(source.id, source.parentSessionId, source.labels);
      if (source.hostId) return { hostId: source.hostId, snapshots };
      id = getSessionParent(source.id);
    }
  } catch {
    // Unknown provenance stays public-only.
  }
  return { hostId: null, snapshots };
}
