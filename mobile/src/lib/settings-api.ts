import { fetchApi } from './api-client';
import type { AppSettingsResponse } from '../types/api';

export const settingsApi = {
  async get(): Promise<AppSettingsResponse> {
    return fetchApi<AppSettingsResponse>('/api/settings');
  },

  async update(key: string, value: boolean | string, persist = true): Promise<{ success?: boolean }> {
    return fetchApi<{ success?: boolean }>('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify({ key, value, persist }),
    });
  },
};
