import type { AdminSession } from '$lib/types';

declare global {
	namespace App {
		interface Locals {
			/** 管理端会话摘要；未登录时为 null。 */
			session: AdminSession | null;
		}
		interface Error {
			message: string;
		}
	}
}

export {};