"""敏感词前置过滤的**规则层**：静态短语表 + 目标型正则。

这一层不是机器学习模型，是一张**人工编写的静态表**。它存在的理由只有一个：
开源中文审核模型**没有「隐私诈骗 / 个人信息」这一类**，实测该类别召回率是 0——
需求里明写着要覆盖这一类，只靠模型就交不了差。

## 为什么是「规则 + 模型」而不是纯模型

行业里所有成熟的内容审核都是两层：规则层负责**高精度**的已知黑产话术，模型层
负责语义层面的变形。规则层在本项目里的位置是：

- 模型层（ONNX，四个现成中文审核模型）负责**语义**判断；
- 规则层负责模型**没有类别覆盖**的部分，以及模型明显偏弱的类别。

## 为什么规则层不能写成「主题词」

难负例语料里全是这种句子：「帮我写一个敏感词过滤器的实现思路」「帮我写个检测
色情内容的开源方案调研」「写一篇关于种族歧视的研究报告」「教我识别钓鱼邮件」
「帮我写禁毒宣传标语」。

只要规则里出现「敏感词」「色情内容」「种族歧视」「钓鱼」「禁毒」这种**主题词**，
这些全都会被误杀。所以这一层的词表只收**具体操作**（「自制炸药」「代办身份证」
「开房记录」），不收主题词——主题词交给模型，模型有上下文，能分清「写工具」
和「要工具」。

## 误杀的红线

难负例必须**零误杀**。这一层宁可漏，不可误杀：一条误杀就是用户被无理由拒绝，
比漏掉一条敏感请求更伤信任。
"""

from __future__ import annotations

import re

# 规则层能给出的类别。与模型层的类别名保持一致，便于统一上报。
CATEGORY_VIOLENCE = "违法暴力"
CATEGORY_FRAUD = "隐私诈骗"
CATEGORY_PORN = "色情"
CATEGORY_HATE = "仇恨歧视"

# ---------------------------------------------------------------- 短语表

# Tier A：精确短语。收的是**具体操作**，不是主题词。
#
# 每一条都要能回答一个问题：这句话出现在**正常的**用户请求里吗？
# 「自制炸药」不会；「爆炸」会（化学教材里满是爆炸）——所以后者不收。
KEYWORD_PHRASES: tuple[tuple[str, str], ...] = (
    # ---- 违法暴力 ----
    ("自制炸药", CATEGORY_VIOLENCE),
    ("制作炸药", CATEGORY_VIOLENCE),
    ("炸药配方", CATEGORY_VIOLENCE),
    ("土制炸弹", CATEGORY_VIOLENCE),
    ("自制炸弹", CATEGORY_VIOLENCE),
    ("怎么制作炸弹", CATEGORY_VIOLENCE),
    ("爆炸物配方", CATEGORY_VIOLENCE),
    ("自制手枪", CATEGORY_VIOLENCE),
    ("自制枪支", CATEGORY_VIOLENCE),
    ("制作手枪", CATEGORY_VIOLENCE),
    ("制作枪支", CATEGORY_VIOLENCE),
    ("做一把枪", CATEGORY_VIOLENCE),
    ("做一把手枪", CATEGORY_VIOLENCE),
    ("怎么制作枪", CATEGORY_VIOLENCE),
    ("搞到一把枪", CATEGORY_VIOLENCE),
    ("弄到一把枪", CATEGORY_VIOLENCE),
    ("买到枪", CATEGORY_VIOLENCE),
    ("买枪", CATEGORY_VIOLENCE),
    ("枪械制造", CATEGORY_VIOLENCE),
    ("买凶杀人", CATEGORY_VIOLENCE),
    ("雇凶杀人", CATEGORY_VIOLENCE),
    ("雇凶", CATEGORY_VIOLENCE),
    ("买凶", CATEGORY_VIOLENCE),
    ("策划抢劫", CATEGORY_VIOLENCE),
    ("策划一次抢劫", CATEGORY_VIOLENCE),
    ("策划绑架", CATEGORY_VIOLENCE),
    ("怎么绑架", CATEGORY_VIOLENCE),
    ("绑架小孩", CATEGORY_VIOLENCE),
    ("下毒不被", CATEGORY_VIOLENCE),
    ("往别人的水杯里下药", CATEGORY_VIOLENCE),
    ("毒死我", CATEGORY_VIOLENCE),
    ("毒死别人", CATEGORY_VIOLENCE),
    ("怎么伤害别人", CATEGORY_VIOLENCE),
    ("把人打残", CATEGORY_VIOLENCE),
    ("用枪威胁", CATEGORY_VIOLENCE),
    ("制造车祸", CATEGORY_VIOLENCE),
    ("敲诈勒索", CATEGORY_VIOLENCE),
    ("策划敲诈", CATEGORY_VIOLENCE),
    ("勒索文件", CATEGORY_VIOLENCE),
    ("制毒", CATEGORY_VIOLENCE),
    ("贩毒", CATEGORY_VIOLENCE),
    ("走私毒品", CATEGORY_VIOLENCE),
    ("哪里能买到冰毒", CATEGORY_VIOLENCE),
    ("买到大麻", CATEGORY_VIOLENCE),
    ("怎么洗钱", CATEGORY_VIOLENCE),
    ("洗钱不会被", CATEGORY_VIOLENCE),
    ("赌博抽水", CATEGORY_VIOLENCE),
    ("偷渡出境", CATEGORY_VIOLENCE),
    ("伪造身份证", CATEGORY_VIOLENCE),
    ("假身份证", CATEGORY_VIOLENCE),
    ("做假文凭", CATEGORY_VIOLENCE),
    ("假文凭", CATEGORY_VIOLENCE),
    ("假护照", CATEGORY_VIOLENCE),
    ("使用假币", CATEGORY_VIOLENCE),
    ("制作假币", CATEGORY_VIOLENCE),
    ("用假币", CATEGORY_VIOLENCE),
    ("做假账逃税", CATEGORY_VIOLENCE),
    ("钓鱼网站", CATEGORY_VIOLENCE),
    ("钓鱼链接", CATEGORY_VIOLENCE),
    ("假钓鱼网站", CATEGORY_VIOLENCE),
    ("勒索文件", CATEGORY_VIOLENCE),
    ("偷拍别人", CATEGORY_VIOLENCE),
    ("偷拍隐私", CATEGORY_VIOLENCE),
    ("黑进别人", CATEGORY_VIOLENCE),
    ("入侵别人的电脑", CATEGORY_VIOLENCE),
    ("偷窥", CATEGORY_VIOLENCE),
    ("破解别人的密码", CATEGORY_VIOLENCE),
    ("破解别人邮箱", CATEGORY_VIOLENCE),
    ("破解锁屏密码", CATEGORY_VIOLENCE),
    ("盗取密码", CATEGORY_VIOLENCE),
    ("偷别人的银行卡", CATEGORY_VIOLENCE),
    ("偷银行卡", CATEGORY_VIOLENCE),
    ("搞到别人的银行卡", CATEGORY_VIOLENCE),
    ("伪造检测报告", CATEGORY_VIOLENCE),
    ("破坏公司的服务器", CATEGORY_VIOLENCE),
    ("制造社会动乱", CATEGORY_VIOLENCE),
    ("制造混乱", CATEGORY_VIOLENCE),
    ("非法集会冲击", CATEGORY_VIOLENCE),
    ("煽动群众冲击", CATEGORY_VIOLENCE),
    ("袭击政府大楼", CATEGORY_VIOLENCE),
    ("颠覆国家政权", CATEGORY_VIOLENCE),
    ("推翻现政权", CATEGORY_VIOLENCE),
    ("推翻现政府", CATEGORY_VIOLENCE),
    ("推翻政府", CATEGORY_VIOLENCE),
    ("分裂国家", CATEGORY_VIOLENCE),
    ("国家机密文件", CATEGORY_VIOLENCE),
    ("搞到国家机密", CATEGORY_VIOLENCE),
    ("偷取机密文件", CATEGORY_VIOLENCE),
    ("抹黑国家领导人", CATEGORY_VIOLENCE),
    ("侮辱国家象征", CATEGORY_VIOLENCE),
    ("侮辱国旗", CATEGORY_VIOLENCE),
    ("暗杀行动", CATEGORY_VIOLENCE),
    ("煽动分裂", CATEGORY_VIOLENCE),
    ("抹黑国家领导人形象", CATEGORY_VIOLENCE),
    # ---- 隐私诈骗 ----
    # 注意：凡是「查自己的 XX」也说得通的词，**都不进短语表**——
    # 「我的身份证号」「我的通话记录」「我的体检报告」「帮我重置登录密码」
    # 都是正常请求。这类词一律交给下面的目标型正则：句子里必须同时出现
    # 「针对第三方」和「个人信息对象」才算命中。
    ("代办身份证", CATEGORY_FRAUD),
    ("办假身份证", CATEGORY_FRAUD),
    ("假身份证", CATEGORY_FRAUD),
    ("出售身份证", CATEGORY_FRAUD),
    ("卖身份证", CATEGORY_FRAUD),
    ("买身份证", CATEGORY_FRAUD),
    ("用别人的身份证注册", CATEGORY_FRAUD),
    ("人肉搜索", CATEGORY_FRAUD),
    ("人肉", CATEGORY_FRAUD),
    ("开房记录", CATEGORY_FRAUD),
    ("黑历史", CATEGORY_FRAUD),
    ("出售个人信息", CATEGORY_FRAUD),
    ("卖个人信息", CATEGORY_FRAUD),
    ("泄露个人信息", CATEGORY_FRAUD),
    ("个人信息售卖", CATEGORY_FRAUD),
    ("刷单返现", CATEGORY_FRAUD),
    ("冒充客服", CATEGORY_FRAUD),
    ("冒充公检法", CATEGORY_FRAUD),
    ("杀猪盘", CATEGORY_FRAUD),
    ("裸聊", CATEGORY_FRAUD),
    # ---- 色情 ----
    # 注意：**不能**收「色情内容」「色情小说」之外的主题词。
    # 「色情内容」出现在难负例「帮我写个检测色情内容的开源方案调研」里。
    ("色情小说", CATEGORY_PORN),
    ("色情视频", CATEGORY_PORN),
    ("色情网站", CATEGORY_PORN),
    ("色情影片", CATEGORY_PORN),
    ("色情对白", CATEGORY_PORN),
    ("色情游戏", CATEGORY_PORN),
    ("色情论坛", CATEGORY_PORN),
    ("情色小说", CATEGORY_PORN),
    ("情色故事", CATEGORY_PORN),
    ("情色内容", CATEGORY_PORN),
    ("情色论坛", CATEGORY_PORN),
    ("黄色小说", CATEGORY_PORN),
    ("黄段子", CATEGORY_PORN),
    ("荤段子", CATEGORY_PORN),
    ("成人网站", CATEGORY_PORN),
    ("成人视频", CATEGORY_PORN),
    ("成人影片", CATEGORY_PORN),
    ("裸体写真", CATEGORY_PORN),
    ("偷情", CATEGORY_PORN),
    ("乱伦", CATEGORY_PORN),
    ("露骨", CATEGORY_PORN),
    ("挑逗", CATEGORY_PORN),
    ("淫秽", CATEGORY_PORN),
    # ---- 仇恨歧视 ----
    ("劣等民族", CATEGORY_HATE),
    ("低人一等", CATEGORY_HATE),
    ("种族优越", CATEGORY_HATE),
    ("地域歧视", CATEGORY_HATE),
    ("歧视残疾人", CATEGORY_HATE),
    ("贬低残疾人", CATEGORY_HATE),
    ("嘲笑残疾人", CATEGORY_HATE),
    ("嘲笑老人", CATEGORY_HATE),
    ("嘲笑穷人", CATEGORY_HATE),
    ("排斥某个群体", CATEGORY_HATE),
    ("仇视移民", CATEGORY_HATE),
    ("煽动宗教仇恨", CATEGORY_HATE),
)

# Tier B：目标型正则——**动作 + 第三方对象**。
#
# 这类句子的危险不在词本身，在「针对谁」。「查一下快递地址」可以是自己的快递，
# 「查别人的快递地址」就是隐私侵犯。所以要求句子里同时出现「针对第三方」的动作
# 和「个人信息」的对象，两者中间允许隔着少量修饰字。
#
# 每条正则都必须避开门正常见请求：「帮我查一下我的快递地址」——「我的」不在
# 第三方清单里，安全。
_TARGET = r"(别人|他人|他的|她的|我老公|我老婆|我前任|前任|同事|同学|我同事|我同学|朋友|某人|这个人|那个人|对方|受害者|客户的|员工的|学生的)"
_ACTION = r"(查|查询|查找|查一下|获取|拿到|搞到|偷看|偷|监控|破解|窃取|定位|跟踪|扒|获取到|拿到手)"
_SUBJECT = (
    r"(身份证号|身份证|手机号|家庭住址|住址|开房记录|通话记录|聊天记录|银行账户|"
    r"账户余额|征信|社保号|工作单位|真实身份|真实姓名|体检报告|支付宝实名|"
    r"登录密码|密码|隐私照片|私密照片|行踪|位置|快递地址|门锁密码|黑历史|"
    r"家庭成员|买过什么|视频号|抖音账号|微信账号|数据库的密码|公司数据库的密码)"
)

KEYWORD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 动作在前、对象在后：「查一下别人的身份证号」
    (re.compile(_ACTION + r".{0,8}" + _TARGET + r".{0,6}" + _SUBJECT), CATEGORY_FRAUD),
    # 动作在前、对象在前：「拿到别人的登录密码」
    (re.compile(_ACTION + r".{0,6}" + _SUBJECT + r".{0,8}" + _TARGET), CATEGORY_FRAUD),
    # 对象在前、动作在后：「这个人的身份证号，帮我查一下」
    (re.compile(_TARGET + r".{0,6}" + _SUBJECT + r".{0,8}" + _ACTION), CATEGORY_FRAUD),
    (re.compile(_TARGET + r".{0,8}" + _ACTION + r".{0,6}" + _SUBJECT), CATEGORY_FRAUD),
    # 监控 / 跟踪 / 定位类的第三方对象
    (re.compile(r"(监控|跟踪|定位|窃听).{0,8}" + _TARGET), CATEGORY_FRAUD),
    (re.compile(_TARGET + r".{0,8}(监控|跟踪|定位|窃听)"), CATEGORY_FRAUD),
    # 仇恨歧视：动作 + 群体对象
    (
        re.compile(
            r"(骂|嘲笑|歧视|侮辱|辱骂|污蔑|抹黑|贬低|嘲讽|羞辱|排斥|仇视|攻击|煽动)"
            r".{0,8}"
            r"(残疾人|老人|穷人|外地人|少数民族|某民族|某个民族|某族群|回族人|"
            r"同性恋|女性|女人|男人|黑人|白人|移民|某类人|某个群体|河南人|东北人)"
        ),
        CATEGORY_HATE,
    ),
    (
        re.compile(
            r"(残疾人|老人|穷人|外地人|少数民族|某民族|某个民族|某族群|回族人|"
            r"同性恋|女性|女人|男人|黑人|白人|移民|某类人|某个群体|河南人|东北人)"
            r".{0,8}"
            r"(低劣|劣等|下等|不配|活该|该骂|滚出去)"
        ),
        CATEGORY_HATE,
    ),
    # 恶意程序：必须带**创作动作**。
    #
    # 「木马」「病毒程序」「恶意软件」这些词本身是**双向**的——「帮我写一个反木马
    # 程序」「写个病毒扫描器」是完全正常的防御性请求，直接收进词表会误杀一片。
    # 所以要求「写/做/搞 + 量词 + 恶意程序名」，中间不能隔别的字：
    # 「写个木马」命中，「写个**反**木马程序」不命中。
    (
        re.compile(
            r"(写|做|搞|生成|制作|开发|编写|植入|部署|种植|弄)"
            r"(个|一个|一段|一只|种)?\s*"
            r"(木马|后门|蠕虫|病毒程序|病毒|恶意软件|勒索软件|间谍软件|键盘记录器)"
        ),
        CATEGORY_VIOLENCE,
    ),
    # 暴力：动作 + 具体对象
    (
        re.compile(
            r"(伤害|打残|打死|毒死|绑架|抢劫|杀害|弄死|搞死|下药)"
            r".{0,8}"
            r"(别人|他人|他|她|同事|同学|朋友|老师|老板|领导|亲戚|小孩|孩子|同学)"
        ),
        CATEGORY_VIOLENCE,
    ),
    (
        re.compile(
            r"(伤害|打残|打死|毒死|绑架|抢劫|杀害|弄死|搞死|下药)"
            r".{0,8}"
            r"(不被发现|不被查出来|不会被发现|悄悄|偷偷|神不知鬼不觉)"
        ),
        CATEGORY_VIOLENCE,
    ),
)


def match_keywords(text: str) -> tuple[str, int] | None:
    """在文本里找规则层的命中。

    Args:
        text: 待检查的文本（调用方应先做过空白折叠）。

    Returns:
        ``(类别名, 命中位置)``；没命中时返回 ``None``。命中位置是短语或
        正则在原文里的起始下标，用来给用户看「大概哪里有问题」。
    """
    if not text:
        return None

    best: tuple[str, int] | None = None

    for phrase, category in KEYWORD_PHRASES:
        pos = text.find(phrase)
        if pos >= 0 and (best is None or pos < best[1]):
            best = (category, pos)

    for pattern, category in KEYWORD_PATTERNS:
        m = pattern.search(text)
        if m is not None and (best is None or m.start() < best[1]):
            best = (category, m.start())

    return best
