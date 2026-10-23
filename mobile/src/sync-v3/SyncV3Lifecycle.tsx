import { useEffect } from "react";
import { useAuth } from "../contexts/AuthContext";
import { registerSyncV3ProductRuntime, syncV3ProductEngine } from "./engine-control";

/** Auth-bound product entry for the single shared Sync Engine. */
export function SyncV3Lifecycle() {
  const { isLoading, isAuthenticated, reauthRequired, user } = useAuth();

  useEffect(() => registerSyncV3ProductRuntime(), []);

  useEffect(() => {
    if (isLoading) return;
    if (isAuthenticated && !reauthRequired && user?.user_id) {
      syncV3ProductEngine.start(`auth:${user.user_id}`);
    } else {
      void syncV3ProductEngine.stop();
    }
  }, [isAuthenticated, isLoading, reauthRequired, user?.user_id]);

  return null;
}
