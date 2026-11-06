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

/** Resolve the nearest own host binding; unlike cache-only getSessionHost in
 * sessionHost.ts, this fetches current metadata for every ancestry hop. */
export async function fetchSessionHostId(
  sessionId: string,
  { queryClient }: { queryClient?: QueryClient } = {},
): Promise<string | null> {
  try {
    const visited = new Set<string>();
    let id: string | null = sessionId;
    while (id !== null && !visited.has(id) && visited.size < MAX_HOST_CHAIN_READS) {
      visited.add(id);
      const hopId: string = id;
      // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
      const source: Session = await getSessionSlim(hopId);
      queryClient?.setQueryData(["session", hopId], source);
      setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
      // Side chats link to their source through labels rather than parentSessionId.
      setSessionParent(source.id, source.parentSessionId, source.labels);
      if (source.hostId) return source.hostId;
      id = getSessionParent(source.id);
    }
  } catch {
    // Unknown provenance stays public-only.
  }
  return null;
}
