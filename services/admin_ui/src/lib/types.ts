/** 与 Python 管理面 API（`/admin/api/*`）对齐的类型。 */

/** 管理员主体。 */
export interface AdminPrincipal {
	kind: string;
	principalId: number;
	username: string;
	tenantId: string | null;
	isOwner: boolean;
	isPlatformSuperAdmin: boolean;
}

/** 界面偏好：主题、色温、密度。 */
export interface AdminPreferences {
	theme: string;
	colorMode: string;
	density: string;
}

/** 导航里的一个页面。 */
export interface AdminPage {
	code: string;
	routeKey: string;
	title: string;
	navGroup: string;
	navOrder: number;
}

/** 会话摘要：身份 + 偏好 + 可见页面 + CSRF 令牌。 */
export interface AdminSession {
	principal: AdminPrincipal;
	preferences: AdminPreferences;
	pages: AdminPage[];
	csrfToken: string;
	components?: Record<string, Record<string, boolean>>;
}

/** 概览页指标。 */
export interface DashboardCounts {
	conversations: number;
	runs: number;
	admins: number;
	platformAdmins: number;
}

/** 概览页载荷。 */
export interface DashboardPayload {
	counts: DashboardCounts;
	components: { metrics: boolean; quickActions: boolean };
}