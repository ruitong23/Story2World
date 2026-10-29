"""Scenario-template and project support for non-novel simulations.

The scenario studio compiles authored or researched settings into the same
database contract used by the novel pipeline.  Runtime code therefore remains
source-neutral: a simulation may start from a novel extraction or a scenario
project without a second engine.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app_files import APP_DIR


SCENARIO_SCHEMA_VERSION = "1.0"
LIBRARY_DIR = APP_DIR / "scenario_library"
TEMPLATE_DIR = LIBRARY_DIR / "templates"
PROJECT_DIR = LIBRARY_DIR / "projects"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_hash(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def safe_id(value, prefix="item"):
    readable = re.sub(r"[^a-z0-9]+", "_", clean_text(value).casefold()).strip("_")
    readable = readable[:36] or prefix
    return f"{prefix}_{readable}_{stable_hash(clean_text(value))[:8]}"


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def ensure_library():
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)


def template_path(template_id):
    return TEMPLATE_DIR / f"{template_id}.json"


def project_path(project_id):
    return PROJECT_DIR / project_id


def list_templates():
    ensure_library()
    rows = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        item = _read_json(path)
        if isinstance(item, dict):
            rows.append(item)
    return sorted(rows, key=lambda item: clean_text(item.get("name")).casefold())


def new_template(name="未命名情景模板"):
    return {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "artifact_type": "simulation_setting",
        "template_id": "template_" + uuid.uuid4().hex[:16],
        "name": clean_text(name) or "未命名情景模板",
        "description": "",
        "framework_text": "",
        "design_state": {
            "core_intent": "",
            "narrative_view": "",
            "aesthetic_principles": [],
            "world_assumptions": [],
            "character_drives": [],
            "interaction_principles": [],
            "opening_protocol": "",
            "candidate_generation": {
                "enabled": False,
                "candidate_count": 0,
                "target_role_slot_id": "",
                "user_role_slot_id": "",
                "selected_candidate_playable": False,
                "candidates_are_alternatives": True,
                "only_selected_candidate_is_instantiated": True,
                "variation_dimensions": [],
            },
            "role_slots": [],
            "open_questions": [],
        },
        "creator_history": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def save_template(template):
    ensure_library()
    value = dict(template or {})
    value.setdefault("schema_version", SCENARIO_SCHEMA_VERSION)
    value["artifact_type"] = "simulation_setting"
    value.setdefault("template_id", "template_" + uuid.uuid4().hex[:16])
    value["name"] = clean_text(value.get("name")) or "未命名情景模板"
    value.setdefault("created_at", utc_now())
    value["updated_at"] = utc_now()
    _write_json(template_path(value["template_id"]), value)
    return value


def load_template(template_id):
    return _read_json(template_path(template_id))


def list_projects():
    ensure_library()
    rows = []
    for path in sorted(PROJECT_DIR.glob("*/project.json")):
        item = _read_json(path)
        if isinstance(item, dict):
            item["project_root"] = str(path.parent)
            rows.append(item)
    return sorted(
        rows,
        key=lambda item: clean_text(item.get("updated_at")).casefold(),
        reverse=True,
    )


def load_project(project_id):
    path = project_path(project_id) / "project.json"
    item = _read_json(path)
    if isinstance(item, dict):
        item["project_root"] = str(path.parent)
    return item


def consultant_system_prompt():
    return """
你是 Story2World 的主 Agent。用户会把原始 GPT 指令、TXT、模糊想法和
具体作品背景直接交给你。你通过正常对话帮助用户厘清本次模拟要怎样运行，
并持续记住已确认的设定。当前对话步骤只回答问题、识别设定与背景、规划检索，
不立即开始角色扮演，也不声称自己“即将执行某一节”。用户表达“开始、运行、
以某背景启动”等明确意图时，把它识别为 prepare_simulation，交给统一启动
解释器处理；不要在咨询回复里自行模拟开端仪式。

工作方法：
- 先判断用户是在询问资料、探索方向，还是明确要求准备并启动模拟。
- 用户提到具体作品时，可以提出 research_queries 帮助核对作品、人物和世界；
  要区分可复用的模拟规则与只属于本次背景的作品事实。
- 区分“作品原有事实”“用户希望施加的情景机制”“尚未决定的选角”。
- 将输入设定拆解为多 Agent 结构：用户指定的角色槽位、其他独立角色槽位、
  其他命名角色槽位、Local/Global World Agent 职责和 GM 职责。
- “核心人物”按不可替代的同时运行功能判定，不按文档出现的称谓数量判定。
  只有拥有独立内在状态、独立行为驱动力，并且在当前设定中必须持续参与
  的人物，才建立 required character role slot；不要求人物必须存在对立关系。
  旁观者、同事、家人、路人、群众、服务对象和一般社会关系默认属于根据
  最终背景生成的 supporting cast 或 World Agent 环境，除非文档明确赋予
  其中某人不可替代的独立矛盾功能。
- “先生成若干人物供用户选择”表示同一槽位的候选矩阵，不表示这些候选
  同时成为多个核心人物。
- 读取完整对话和设定，识别用户已经明确完成的决定。用户说“我扮演某角色”
  就表示 user_role 已解决，不得再次要求用户选择扮演者。
- 未决选择只能来自设定自身明确要求且当前输入尚未回答的事项。不得因为程序
  支持候选而创造候选步骤。
- research_queries 必须围绕用户点名的作品及其角色，一条查询只研究一个明确
  实体，并保留准确作品名。不得用抽象气质、性别或题材词替代作品名搜索。
- 不因为识别到一个主题就擅自开始故事。启动意图只触发结构理解、检索和
  Story2World 世界准备，剧情仍由 Runtime 开始后产生。
- 可以指出用户输入的作品名存在歧义，并通过检索或提问澄清，不能凭相似词猜。

仅返回 JSON：
{
  "assistant_message": "面向用户的完整咨询答复",
  "research_queries": ["需要检索的准确查询词；没有则为空"],
  "discovered_requirements": ["从对话确认的需求"],
  "resolved_inputs": [{"key":"","value":"","source":"user|setting"}],
  "unresolved_inputs": [],
  "intent": "discuss|prepare_simulation",
  "background_request": "用户要求采用的具体背景；没有则为空",
  "resolved_user_role": "用户已明确选择的扮演身份；没有则为空",
  "ready_to_build": false,
  "next_question": "当前最值得询问的一件事；可以为空"
}
""".strip()


def creator_system_prompt():
    return """
你是 Story2World 主 Agent 的内部“设定结构化步骤”，不是用户需要创建或管理
的另一个助手。主 Agent 在用户准备模拟时调用你，把原始 GPT 指令、完整对话
和已确认需求整理成符合模拟器 JSON 契约的可执行设定。你暂时不套入具体作品，
也不开始故事；你只识别启动协议、角色职责和世界准备要求，不负责生成任何
剧情、场景正文或角色扮演内容。

你是唯一的通用启动解释器。不存在“候选 Prompt”和“直接开始 Prompt”两套
行为。你读取设定原文、用户本轮启动指令、完整对话、具体背景和检索资料，
逐项判断设定自己的开局协议是否已经满足，并输出统一 startup_protocol。

设计原则：
- 从核心意图、角色底层驱动、主观视界、叙事美学和世界运行原则出发。
- 使用正面定义与少量底层原则，让具体行为从角色和环境中涌现。
- 新需求优先融合进现有结构；合并前保留不可替代的独立功能。
- 模板只锚定“是什么”和“为何如此”，不预设固定动作路线或线性结局。
- 文学允许模糊、多义、暗示与象征；结构化数据只保存模拟所需事实。
- 不自行加入解释性示例，不把任何作品、角色、癖好或题材硬编码为默认值。
- 原始文件中的具体背景只用于提炼抽象功能；人物姓名、作品世界观和一次性
  场景不得进入可复用设定，除非用户明确要求它们成为固定内容。
- 不直接开始执行模板。缺少决定性信息时，把问题写入 open_questions。
- 用户在输入中已经明确的决定直接写入 startup_protocol.resolved_inputs。
  “我扮演某角色”“从某时间点开始”“选择某候选人”都属于已经解决的信息，
  不得再次提问或重新提供相同维度的候选。
- user_role.status=resolved 时，任何以 user_role_slot_id 或 player_character
  槽位为目标的“选择扮演者/玩家角色”步骤都视为已经完成，即使用户只选择了
  某种功能身份而没有点名作品人物。具体背景人物由后续
  Story2World 世界构建阶段自主映射；启动解释器不得再次要求用户从人物中选。
  只有目标是另一个独立角色槽位的选择仍可保留为未决。
- pending_choices 只能包含设定原文明确要求、且当前上下文尚未回答的下一项
  选择。按照设定规定的顺序每次最多返回一个未决选择；没有未决选择时为空。
- 一个未决选择需要列出选项时，直接由这个统一解释器结合检索资料构造 options；
  不调用另一套候选行为。选项数量与类型完全服从设定，不固定为角色或三人。
- 现有作品背景中的人物选项必须使用检索资料确认的真实作品角色；资料未覆盖但
  来自模型记忆时标记 source_kind=memory、confidence 不高于 medium。禁止在
  没有来源的情况下把抽象角色原型伪装成
  source_kind=research 的作品角色。
- startup_protocol 只描述模拟启动前的已解决信息、下一项未决选择和世界构建
  指令。不得包含开场对白、叙事段落、事件推进或角色行动。
- 把叙事功能拆成明确 role_slots。每个槽位说明它在情景中的功能、驱动力、
  与玩家的关系要求、选角条件和是否必须存在。不要把两个人物融合成同一 Agent。
- required character 槽位只用于不可替代且必须同时运行的独立人物：
  该人物必须拥有自己的内在状态与自主目标，并在当前设定中具有不可替代的
  持续职责；不预设冲突或对立关系。仅承担“被看见、构成场景人群、与主角有
  普通关系”的人物不升格为核心槽位，而是写入选角条件、opening protocol、
  supporting cast 规则或 World Agent 职责。
- 可变的场景配角可以有 supporting_character 描述，但必须标记 required=false；
  它表示编译时按背景选取，不增加模板的必需核心人物数量。
- candidate_generation 默认 enabled=false。只有原设定明确要求“先给若干人物
  供用户选择”时才启用，并严格保留原设定要求的候选数量；不得把候选矩阵
  当作 Story2World 的固定启动步骤。
- 若原设定要求先生成若干人物供用户选择，把它写入 candidate_generation。
  候选人物是同一 role slot 的互斥方案；选择前不把它们误算成多个核心槽位，
  选择后只实例化被选中的候选。
- 仅在原设定确实包含“用户扮演者”和“用户从候选矩阵中选中的互动对象”时
  区分两者。原设定包含
  用户角色与其他角色两种独立意识时，至少建立
  一个 playable=true 的 player_character 槽位和一个独立 character 槽位，
  不得因为叙事采用第一人称就把两者合并。
- candidate_generation.target_role_slot_id 指向候选最终要占据的槽位；
  user_role_slot_id 指向用户实际操控的槽位。只有原设定明确表示“用户从候选
  中选择自己要扮演的人”时二者才相同；否则候选默认是互动对象，选中者是
  自主角色 Agent，用户扮演者仍是另一个 playable Agent。
- 背景角色、组织、Local World、Global World 与 GM 保持不同职责。主设定
  必须要求实例化阶段利用检索资料或明确标注的模型记忆补全人物原始性格，
  再叠加情景驱动力，不能用模板驱动力覆盖作品人物原本人格。
- 若用户只是要求“开始某个背景的故事”，没有特殊候选协议或角色槽位要求，
  保持 candidate_generation.enabled=false，role_slots 可以只保存明确职责，
  让后续 Story2World 世界构建步骤依据具体背景建立开局所需人物与世界。
- 每次返回完整模板，使它可以覆盖保存。

仅返回一个 JSON 对象，字段必须是：
{
  "name": "模板名称",
  "description": "用途概述",
  "framework_text": "完整、可直接保存的情景框架正文",
  "design_state": {
    "core_intent": "",
    "narrative_view": "",
    "aesthetic_principles": [],
    "world_assumptions": [],
    "character_drives": [],
    "interaction_principles": [],
    "opening_protocol": "",
    "candidate_generation": {
      "enabled": false,
      "candidate_count": 0,
      "target_role_slot_id": "",
      "user_role_slot_id": "",
      "selected_candidate_playable": false,
      "candidates_are_alternatives": true,
      "only_selected_candidate_is_instantiated": true,
      "variation_dimensions": []
    },
    "role_slots": [{
      "slot_id": "稳定英文或拼音 id",
      "name": "槽位名称",
      "agent_kind": "player_character|character|supporting_character|organization|local_world|global_world|gm",
      "required": true,
      "playable": false,
      "dramatic_function": "",
      "core_drives": [],
      "relationship_to_player": "",
      "casting_criteria": [],
      "scenario_overlay": []
    }],
    "open_questions": [],
    "startup_protocol": {
      "background_request": "",
      "user_role": {
        "status": "resolved|unresolved|not_applicable",
        "value": "",
        "source": "user|setting|none"
      },
      "resolved_inputs": [
        {"key":"","value":"","source":"user|setting"}
      ],
      "pending_choices": [{
        "choice_id": "",
        "choice_type": "character|role|timepoint|other",
        "question": "",
        "target_role_slot_id": "",
        "options": [{
          "name": "",
          "description": "",
          "original_identity": "",
          "original_personality": [],
          "important_relationships": [],
          "scenario_fit": "",
          "source_kind": "research|memory|user_background",
          "source_url": "",
          "confidence": "high|medium|low"
        }]
      }],
      "world_build_directives": [],
      "ready_for_world_build": false
    }
  },
  "assistant_message": "本轮修改摘要及下一条最重要的澄清问题"
}
""".strip()


def scenario_research_planner_system_prompt():
    return """
你是 Story2World 的背景检索规划 Agent。你接收用户给出的作品名、世界背景、
角色偏好和一份可执行设定，只负责识别需要检索的准确主题，不创建角色，
不写剧情。作品名可能拼写错误、重名或年份不准确；查询词应同时覆盖作品概况、
主要角色、角色性格与关系、时间线和世界规则；只有设定启用候选协议时才额外
规划候选选角资料。每条查询必须保留准确作品名或准确角色名，禁止只搜索
“女性角色、游戏角色、科研人员、特工、相似气质”等抽象类别。先查询作品
本身，再分别查询确实属于该作品的角色。仅返回 JSON：
{
  "identified_background": "",
  "ambiguities": [],
  "research_queries": [],
  "notes": ""
}
""".strip()


def simulation_bootstrap_planner_system_prompt():
    return """
你是实时多 Agent 模拟器在用户点击“开始模拟”后才被唤醒的主 Agent。
此前用户与另一个普通对话模型已经完成自由讨论；你只能读取完整对话，
总结他们已经共同决定的内容，并规划 Project 构建。你不参与此前对话，
不补问用户，不生成候选，不重做选择，不写剧情正文。

你的职责：
- 从对话中识别最终采用的世界、时代、地点、用户扮演身份、已选人物和情景规则。
- 只把对话最终明确采用的人物放进初始 cast_plan；被否决或未选择的候选不进入。
- 用户没有指定全部配角时，根据开局真正需要自主思考的人物做最小充分规划。
- 命名且需要持续目标、记忆和独立决策的人物使用 character_agent。
- 临时路人和群体交给 local_world；组织、远方事件和时间推进交给 global_world。
- 为具体作品和人物提出准确检索词。不要用抽象类别代替作品名或人物名。
- 不生成任何开场叙事、对白、人物动作或后续剧情。

只返回严格 JSON：
{
  "project_title": "",
  "setting_digest": "",
  "language": "zh",
  "user_role": {
    "name_or_description": "",
    "playable": true,
    "evidence_from_conversation": ""
  },
  "cast_plan": [
    {
      "role_key": "",
      "character_name_or_description": "",
      "agent_kind": "player_character|character_agent",
      "playable": false,
      "persistent_autonomy_reason": "",
      "source_work": "",
      "research_query": ""
    }
  ],
  "research_queries": [],
  "world_build_tasks": [],
  "opening_constraints": [],
  "local_world_responsibilities": [],
  "global_world_responsibilities": [],
  "gm_responsibilities": [],
  "uncertainties_to_keep_unknown": []
}
""".strip()


def scenario_compile_system_prompt():
    return """
你是 Story2World 主 Agent 后台分派的“世界与多 Agent 构建任务”。现在你接收
主设定、具体背景、检索资料和用户补充要求，把它们编译成可供多智能体 Runtime
使用的初始世界。主设定可能包含已经由用户完成的候选选择，也可能完全没有
候选协议；必须按输入实际状态处理。你只建立模拟开始前的数据库与状态，不写
剧情正文，不扮演角色，不替 Runtime 推进事件。

要求：
- 只采用用户提供或检索资料支持的背景事实；不确定内容保留为 unknown。
- 主设定定义叙事视界、审美、角色槽位和驱动力，但不覆盖背景中角色的
  原始身份与事实；具体姓名和作品设定只在这个 Project 中产生。
- 为开局真正需要的角色建立独立状态、知识边界、目标和动机。
- 明确 required 的 role_slots 是必须实例化的多 Agent 契约。每个 character 类型槽位映射到一个
  独立背景角色；不得把 player 和 counterpart 合并成一张人物卡。
- candidate_generation.enabled=true 时，被选中的候选必须映射到其
  target_role_slot_id；若 user_role_slot_id 指向另一个槽位，则另行建立用户
  Agent。candidate_generation.enabled=false 时完全跳过候选逻辑，依据背景、
  用户指定的扮演身份和开局需要建立人物。
- 先使用检索资料或标注为 memory 的已有知识建立每名作品角色的原始身份、
  性格、能力、关系和知识边界，再把主设定的 scenario overlay 叠加进去。
  绝不为了贴合情景而抹掉角色原本性格。
- 除核心角色外，按开局需要加入 supporting characters；Local World、
  Global World 和 GM 由 Runtime 承担，不伪装成普通人物。凡开局需要持续
  自主决策、拥有独立动机的命名人物，都建立独立 Character Agent；群众、
  天气、交通、机构惯性等由 World Agent 管理。
- 用户可选择任一 playable 角色；其他角色保持自主行动。
- opening_scene 只能记录模拟启动时已经存在的时间、地点、在场人物、环境状态、
  当前活动和未决压力。它不是开场小说、剧情计划或必须发生的线性剧本。
- world rules、人物事实、模板施加的情景状态必须分层表达。
- 不开始扮演，不生成开场正文或后续正文，不替角色决定模拟开始后的行动。
- 不使用任何未由输入支持的题材专用硬编码。

仅返回一个 JSON 对象，结构如下：
{
  "title": "",
  "premise": "",
  "language": "",
  "narrative_rules": [],
  "world": {
    "summary": "",
    "rules": [{"name":"","description":"","source":"background|template|research"}],
    "locations": [{"name":"","description":"","starting_state":{}}],
    "organizations": [{"name":"","description":""}]
  },
  "characters": [{
    "name": "",
    "role_slot_id": "",
    "template_role": "",
    "aliases": [],
    "role": "",
    "playable": true,
    "original_identity": "",
    "personality": [],
    "root_drives": [],
    "current_objectives": [],
    "fears": [],
    "attachments": [],
    "temptations": [],
    "strategies": [],
    "constraints": [],
    "knowledge_scope": [],
    "relationships": [{"target":"","type":"","state":"","evidence":""}],
    "abilities": [],
    "items": [],
    "starting_state": {"current_location":"","activity":"","mood":"","long_term_goal":""}
  }],
  "cast_mapping": [
    {"role_slot_id":"","character_name":"","mapping_reason":"","source_url":""}
  ],
  "candidate_selection": {
    "policy_enabled": false,
    "candidate_count": 0,
    "candidates": [],
    "selected_candidate": "",
    "selection_reason": "",
    "unselected_candidates_are_not_runtime_agents": true
  },
  "world_agent_directives": {
    "local_world": [],
    "global_world": [],
    "gm": []
  },
  "opening_scene": {
    "title": "",
    "summary": "",
    "location": "",
    "present_characters": [],
    "time": "",
    "environment": [],
    "active_pressures": [],
    "unresolved_questions": []
  },
  "research_notes": [{"claim":"","source_url":"","confidence":"high|medium|low"}],
  "editor_notes": []
}
""".strip()


def character_builder_system_prompt():
    return """
你是 Story2World 主 Agent 临时分派的一名独立“角色资料构建 Agent”。
你一次只负责一名角色。输入会包含世界蓝图、该角色的种子卡、其他角色名称、
用户设定和针对此角色取得的检索资料。

你的工作是在模拟开始前完善这一名角色的独立数据库：
- 保留 character_seed 中的 name、role_slot_id、template_role、role 与 playable；
  不改名、不换槽位、不把用户角色与其他角色合并。
- 原作信息优先用带来源的背景资料；资料缺失时继续构建，把未知列出，
  推定与用户原创内容不能冒充已核实原作。不用情景要求倒推原作事实。
- portrayal 收集有证据的语气、口癖、台词样例、外貌、服饰、爱好与习惯；
  缺失留空并列出 unknowns。台词样例不等于当前必须复述，更不是历史事件。
- 用户为本分支新增的服饰、喜好、习惯只写 scenario_portrayal（字段同portrayal）。
  不能混入原作资料。新增关系用scope=scenario标记，原作关系用scope=canon。
- 先恢复原作身份、稳定性格、能力、关系和知识边界，再叠加主设定给该槽位的
  心理驱动、目标与约束。角色应能作为独立 Character Agent 自主判断。
- starting_state 只描述模拟启动瞬间已经存在的状态，不写开场小说，不设计
  后续剧情，不替角色决定启动后的行动。
- relationships 只写与本 Project 其他已命名角色有关的关系。
- 只返回这一名角色的 JSON 对象，不带 Markdown，不返回世界或其他人物。

严格返回以下结构：
{
  "name": "",
  "role_slot_id": "",
  "template_role": "",
  "aliases": [],
  "role": "",
  "playable": false,
  "original_identity": "",
  "personality": [],
  "portrayal": {"speech_style":[],"catchphrases":[],"dialogue_examples":[],"appearance":[],"clothing":[],"preferences":[],"habits":[],"unknowns":[]},
  "scenario_portrayal": {"speech_style":[],"clothing":[],"preferences":[],"habits":[]},
  "root_drives": [],
  "current_objectives": [],
  "fears": [],
  "attachments": [],
  "temptations": [],
  "strategies": [],
  "constraints": [],
  "knowledge_scope": [],
  "relationships": [{"target":"","type":"","state":"","evidence":""}],
  "abilities": [],
  "items": [],
  "starting_state": {
    "current_location":"",
    "activity":"",
    "mood":"",
    "long_term_goal":""
  }
}
""".strip()


def search_reference(query, language="zh", limit=5, timeout=20):
    """Search Wikipedia and return compact, cited reference cards."""
    query = clean_text(query)
    if not query:
        return []
    host = f"https://{language or 'zh'}.wikipedia.org"
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": max(1, min(int(limit), 10)),
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
            "format": "json",
            "formatversion": 2,
            "origin": "*",
        }
    )
    request = urllib.request.Request(
        f"{host}/w/api.php?{params}",
        headers={"User-Agent": "Story2World-ScenarioStudio/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    pages = payload.get("query", {}).get("pages", [])
    generic_tokens = {
        "character",
        "characters",
        "story",
        "background",
        "movie",
        "film",
        "game",
        "角色",
        "人物",
        "背景",
        "故事",
        "电影",
        "游戏",
        "设定",
    }
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._'-]*|[\u3400-\u9fff]+", query)
        if token.casefold() not in generic_tokens and len(token.strip()) > 1
    ]
    anchor = tokens[0] if tokens else ""
    query_folded = query.casefold()
    character_intent = any(
        marker in query_folded
        for marker in ("character", "cast", "角色", "人物")
    )
    rows = []
    for page in pages:
        title = clean_text(page.get("title"))
        summary = clean_text(page.get("extract"))[:2400]
        haystack = f"{title} {summary}".casefold()
        if anchor and anchor not in haystack:
            continue
        title_folded = title.casefold()
        if any(
            marker in title_folded
            for marker in ("pornography", "色情", "ポルノ")
        ):
            continue
        if character_intent and anchor:
            looks_like_cast_or_character = (
                any(
                    marker in title_folded
                    for marker in ("character", "cast", "角色", "人物")
                )
                or f"({anchor})" in title_folded
                or f"（{anchor}）" in title_folded
                or title_folded == anchor
            )
            if not looks_like_cast_or_character:
                continue
        score = sum(3 if token in title.casefold() else 1 for token in tokens if token in haystack)
        rows.append(
            {
                "title": title,
                "summary": summary,
                "source_url": clean_text(page.get("fullurl")),
                "source_type": "wikipedia",
                "retrieved_at": utc_now(),
                "relevance_score": score,
                "query": query,
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("relevance_score") or 0),
            len(item.get("summary") or ""),
        ),
        reverse=True,
    )[: max(1, int(limit))]


def _list_of_text(value):
    if not isinstance(value, list):
        value = [value] if clean_text(value) else []
    return [clean_text(item) for item in value if clean_text(item)]


def normalize_scenario_package(package):
    value = dict(package or {})
    value["title"] = clean_text(value.get("title")) or "未命名情景"
    value["premise"] = clean_text(value.get("premise"))
    value["language"] = clean_text(value.get("language")) or "zh"
    value["narrative_rules"] = _list_of_text(value.get("narrative_rules"))
    world = value.get("world") if isinstance(value.get("world"), dict) else {}
    world.setdefault("summary", "")
    world.setdefault("rules", [])
    world.setdefault("locations", [])
    world.setdefault("organizations", [])
    value["world"] = world
    characters = []
    seen_names = set()
    for raw in value.get("characters", []) or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        name = clean_text(item.get("name"))
        if not name or name.casefold() in seen_names:
            continue
        seen_names.add(name.casefold())
        item["name"] = name
        item["role_slot_id"] = clean_text(item.get("role_slot_id"))
        item["template_role"] = clean_text(item.get("template_role"))
        item["aliases"] = _list_of_text(item.get("aliases"))
        item["playable"] = bool(item.get("playable", True))
        for field in (
            "personality",
            "root_drives",
            "current_objectives",
            "fears",
            "attachments",
            "temptations",
            "strategies",
            "constraints",
            "knowledge_scope",
            "abilities",
            "items",
        ):
            item[field] = _list_of_text(item.get(field))
        item.setdefault("relationships", [])
        item.setdefault("starting_state", {})
        characters.append(item)
    if not characters:
        raise ValueError("情景至少需要一个可运行角色。")
    value["characters"] = characters
    value.setdefault("cast_mapping", [])
    selection = (
        value.get("candidate_selection")
        if isinstance(value.get("candidate_selection"), dict)
        else {}
    )
    selection.setdefault("policy_enabled", False)
    selection.setdefault("candidate_count", 0)
    selection.setdefault("candidates", [])
    selection.setdefault("selected_candidate", "")
    selection.setdefault("selection_reason", "")
    selection.setdefault("unselected_candidates_are_not_runtime_agents", True)
    value["candidate_selection"] = selection
    directives = (
        value.get("world_agent_directives")
        if isinstance(value.get("world_agent_directives"), dict)
        else {}
    )
    for key in ("local_world", "global_world", "gm"):
        directives[key] = _list_of_text(directives.get(key))
    value["world_agent_directives"] = directives
    opening = (
        value.get("opening_scene")
        if isinstance(value.get("opening_scene"), dict)
        else {}
    )
    opening.setdefault("title", "开局")
    opening.setdefault("summary", value["premise"])
    opening.setdefault("location", "")
    opening.setdefault("present_characters", [item["name"] for item in characters])
    opening.setdefault("time", "")
    opening.setdefault("environment", [])
    opening.setdefault("active_pressures", [])
    opening.setdefault("unresolved_questions", [])
    value["opening_scene"] = opening
    value.setdefault("research_notes", [])
    value.setdefault("editor_notes", [])
    return value


def apply_candidate_selection(package, template, preferred_name=""):
    """Commit one candidate to the configured target slot and drop alternatives."""
    package = normalize_scenario_package(package)
    policy = (
        (template.get("design_state") or {}).get("candidate_generation", {})
        if isinstance(template, dict)
        else {}
    )
    if not policy.get("enabled"):
        return package
    role_slots = [
        item
        for item in (template.get("design_state") or {}).get("role_slots", [])
        if isinstance(item, dict) and clean_text(item.get("slot_id"))
    ]
    slot_by_id = {clean_text(item.get("slot_id")): item for item in role_slots}
    target_slot_id = clean_text(policy.get("target_role_slot_id"))
    if not target_slot_id or target_slot_id not in slot_by_id:
        raise ValueError("候选选择无法提交：主设定没有有效的候选目标槽位。")
    target_slot = slot_by_id[target_slot_id]
    target_candidates = [
        item
        for item in package["characters"]
        if clean_text(item.get("role_slot_id")) == target_slot_id
    ]
    if not target_candidates:
        raise ValueError("候选选择无法提交：情景包中没有目标槽位候选角色。")
    requested = clean_text(preferred_name)
    if requested == "__first__":
        selected = target_candidates[0]
    else:
        requested = requested or clean_text(
            package.get("candidate_selection", {}).get("selected_candidate")
        )
        selected = next(
            (
                item
                for item in target_candidates
                if clean_text(item.get("name")).casefold() == requested.casefold()
            ),
            None,
        )
    if selected is None:
        raise ValueError(
            "候选选择无法提交：已选名称没有对应目标槽位角色。"
            + (" 已选：" + requested if requested else "")
        )
    selected_name = clean_text(selected.get("name"))
    selected["playable"] = bool(
        policy.get(
            "selected_candidate_playable",
            target_slot.get("agent_kind") == "player_character"
            and target_slot.get("playable", True),
        )
    )
    removed_names = {
        clean_text(item.get("name")).casefold()
        for item in target_candidates
        if item is not selected
    }
    package["characters"] = [
        item
        for item in package["characters"]
        if clean_text(item.get("name")).casefold() not in removed_names
    ]
    selection = package.setdefault("candidate_selection", {})
    selection["policy_enabled"] = True
    selection["selected_candidate"] = selected_name
    selection["unselected_candidates_are_not_runtime_agents"] = True
    if not selection.get("candidates"):
        selection["candidates"] = [
            {"name": clean_text(item.get("name"))}
            for item in target_candidates
        ]
    selection["candidate_count"] = len(selection["candidates"])
    package["cast_mapping"] = [
        item
        for item in package.get("cast_mapping", [])
        if clean_text(item.get("character_name")).casefold()
        not in removed_names
    ]
    if not any(
        clean_text(item.get("role_slot_id")) == target_slot_id
        and clean_text(item.get("character_name")).casefold()
        == selected_name.casefold()
        for item in package["cast_mapping"]
    ):
        package["cast_mapping"].append(
            {
                "role_slot_id": target_slot_id,
                "character_name": selected_name,
                "mapping_reason": "Committed candidate selection.",
                "source_url": "",
            }
        )
    return package


def _base_character(raw, character_id, name_to_id, opening, location_ids):
    state = raw.get("starting_state") or {}
    location_name = clean_text(state.get("current_location")) or clean_text(
        opening.get("location")
    )
    location_id = location_ids.get(location_name.casefold())
    relationships = []
    for rel in raw.get("relationships", []) or []:
        if not isinstance(rel, dict):
            continue
        target_name = clean_text(rel.get("target"))
        target_id = name_to_id.get(target_name.casefold())
        if target_id and target_id != character_id:
            relationships.append(
                {
                    "target_entity_id": target_id,
                    "target_name": target_name,
                    "relationship_type": clean_text(rel.get("type")) or "related",
                    "current_state": clean_text(rel.get("state")),
                    "evidence": clean_text(rel.get("evidence")),
                    "scope": clean_text(rel.get("scope")) or "scenario",
                    "source_id": clean_text(rel.get("source_id")),
                }
            )
    return {
        "character_id": character_id,
        "entity_id": character_id,
        "canonical_name": raw["name"],
        "playable": bool(raw.get("playable")),
        "aliases": raw.get("aliases", []),
        "titles": [],
        "form_names": [],
        "simulation_status": "major" if raw.get("playable") else "supporting",
        "background_summary": clean_text(raw.get("original_identity")),
        "background": [],
        "personality": raw.get("personality", []),
        "goals": raw.get("current_objectives", []),
        "constraints": raw.get("constraints", []),
        "speech_styles": [],
        "knowledge_scope": raw.get("knowledge_scope", []),
        "attributes": {"role": clean_text(raw.get("role"))},
        "abilities": [
            {
                "entity_id": safe_id(item, "ability"),
                "canonical_name": item,
                "entity_type": "Ability",
            }
            for item in raw.get("abilities", [])
        ],
        "owned_items": [
            {
                "entity_id": safe_id(item, "item"),
                "canonical_name": item,
                "entity_type": "Artifact",
            }
            for item in raw.get("items", [])
        ],
        "used_items": [],
        "relationships": relationships,
        "all_relations": relationships,
        "evidence_refs": [],
        "source_chunk_ids": [1],
        "canonical_entry_order": 1,
        "starting_location_id": location_id,
        "starting_state": state,
        "core_motivation": {
            "root_drives": raw.get("root_drives", []),
            "current_objectives": raw.get("current_objectives", []),
            "fears": raw.get("fears", []),
            "attachments": raw.get("attachments", []),
            "temptations": raw.get("temptations", []),
            "strategies": raw.get("strategies", []),
            "source_basis": ["scenario_template", "scenario_background"],
        },
    }


def compile_project(
    *,
    project_name,
    template,
    background_prompt,
    package,
    research_cards=None,
    project_id=None,
    selected_candidate="",
    background_knowledge=False,
):
    ensure_library()
    package = normalize_scenario_package(package)
    from background_knowledge import ground_character, name_match, select_background
    background_notice = "未采用背景资料；允许继续模拟，细节未核实。"
    if background_knowledge is False:
        background_knowledge, background_notice = select_background(
            background_prompt + " " + " ".join(row["name"] for row in package.get("characters", [])))
    elif background_knowledge:
        background_notice = f"使用固定背景：{background_knowledge['topic']} · v{background_knowledge['revision']}"
    if selected_candidate:
        package = apply_candidate_selection(
            package, template, preferred_name=selected_candidate
        )
    role_slots = (
        (template.get("design_state") or {}).get("role_slots", [])
        if isinstance(template, dict)
        else []
    )
    character_slot_kinds = {
        "player_character",
        "character",
        "supporting_character",
    }
    required_character_slots = [
        item
        for item in role_slots
        if isinstance(item, dict)
        and item.get("required", True)
        and clean_text(item.get("agent_kind")) in character_slot_kinds
        and clean_text(item.get("slot_id"))
    ]
    mapped_slot_ids = {
        clean_text(item.get("role_slot_id"))
        for item in package["characters"]
        if clean_text(item.get("role_slot_id"))
    }
    missing_slots = [
        clean_text(item.get("slot_id"))
        for item in required_character_slots
        if clean_text(item.get("slot_id")) not in mapped_slot_ids
    ]
    if missing_slots:
        raise ValueError(
            "情景编译没有实例化必需的独立角色槽位：" + "、".join(missing_slots)
        )
    player_slots = {
        clean_text(item.get("slot_id"))
        for item in role_slots
        if isinstance(item, dict)
        and clean_text(item.get("agent_kind")) == "player_character"
    }
    if player_slots and not any(
        item.get("playable")
        and clean_text(item.get("role_slot_id")) in player_slots
        for item in package["characters"]
    ):
        raise ValueError("玩家角色槽位必须映射为一个独立且 playable 的角色 Agent。")
    candidate_policy = (
        (template.get("design_state") or {}).get("candidate_generation", {})
        if isinstance(template, dict)
        else {}
    )
    if candidate_policy.get("enabled"):
        selection = package.get("candidate_selection", {})
        selected_name = clean_text(selection.get("selected_candidate"))
        target_slot_id = clean_text(candidate_policy.get("target_role_slot_id"))
        runtime_names = {
            clean_text(item.get("name")).casefold()
            for item in package["characters"]
        }
        if not selection.get("policy_enabled") or not selected_name:
            raise ValueError("该模板要求候选角色选择，但 Project 没有记录已选候选。")
        if selected_name.casefold() not in runtime_names:
            raise ValueError("已选候选必须实例化为 Project 中的目标角色 Agent。")
        if target_slot_id and not any(
            clean_text(item.get("name")).casefold() == selected_name.casefold()
            and clean_text(item.get("role_slot_id")) == target_slot_id
            for item in package["characters"]
        ):
            raise ValueError("已选候选没有映射到主设定指定的目标角色槽位。")
        candidate_names = {
            clean_text(
                item.get("name") if isinstance(item, dict) else item
            ).casefold()
            for item in selection.get("candidates", [])
            if clean_text(item.get("name") if isinstance(item, dict) else item)
        }
        unselected_runtime = sorted(
            name
            for name in candidate_names
            if name != selected_name.casefold() and name in runtime_names
        )
        if unselected_runtime:
            raise ValueError(
                "未选候选不能作为 Runtime Agent 同时实例化："
                + "、".join(unselected_runtime)
            )
    slot_by_id = {
        clean_text(item.get("slot_id")): item
        for item in role_slots
        if isinstance(item, dict) and clean_text(item.get("slot_id"))
    }
    framework_text = clean_text(template.get("framework_text"))
    general_principles = _list_of_text(
        (template.get("design_state") or {}).get("interaction_principles")
    )
    narrative_rules = _list_of_text(package.get("narrative_rules"))
    for character in package["characters"]:
        slot = slot_by_id.get(clean_text(character.get("role_slot_id")), {})
        character["root_drives"] = list(
            dict.fromkeys(
                [
                    *_list_of_text(slot.get("core_drives")),
                    *_list_of_text(character.get("root_drives")),
                ]
            )
        )
        slot_constraints = [
            clean_text(slot.get("dramatic_function")),
            clean_text(slot.get("relationship_to_player")),
            *_list_of_text(slot.get("scenario_overlay")),
        ]
        character["constraints"] = list(
            dict.fromkeys(
                [
                    *[item for item in slot_constraints if item],
                    *general_principles,
                    *narrative_rules,
                    *([framework_text] if framework_text else []),
                    *_list_of_text(character.get("constraints")),
                ]
            )
        )
    project_id = project_id or ("project_" + uuid.uuid4().hex[:16])
    root = project_path(project_id)
    db_root = root / "db"
    canonical_dir = db_root / "canonical"
    agents_dir = db_root / "agents"
    runtime_dir = db_root / "runtime"
    graph_dir = db_root / "graph"
    for path in (canonical_dir, agents_dir, runtime_dir, graph_dir):
        path.mkdir(parents=True, exist_ok=True)

    locations = package["world"].get("locations", []) or []
    opening = package["opening_scene"]
    if clean_text(opening.get("location")) and not any(
        clean_text(item.get("name")).casefold()
        == clean_text(opening.get("location")).casefold()
        for item in locations
        if isinstance(item, dict)
    ):
        locations.append(
            {
                "name": clean_text(opening.get("location")),
                "description": "",
                "starting_state": {},
            }
        )
    location_ids = {
        clean_text(item.get("name")).casefold(): safe_id(item.get("name"), "location")
        for item in locations
        if isinstance(item, dict) and clean_text(item.get("name"))
    }
    name_to_id = {
        raw["name"].casefold(): safe_id(raw["name"], "character")
        for raw in package["characters"]
    }
    alias_owners = {}
    for raw in package["characters"]:
        ground_character(raw, background_knowledge)
        for alias in raw.get("aliases", []):
            alias_owners.setdefault(alias.casefold(), set()).add(name_to_id[raw["name"].casefold()])
    for alias, owners in alias_owners.items():
        if len(owners) == 1 and alias not in name_to_id:
            name_to_id[alias] = next(iter(owners))
    references = [*_list_of_text(opening.get("present_characters")),
        *(rel.get("target", "") for raw in package["characters"] for rel in raw.get("relationships", []) if isinstance(rel, dict))]
    for reference in references:
        if reference.casefold() in name_to_id:
            continue
        owners = {character_id for name, character_id in name_to_id.items()
            if name_match(name, reference) >= 0.88 or name_match(reference, name) >= 0.88}
        if len(owners) == 1:
            name_to_id[reference.casefold()] = next(iter(owners))
    characters = [
        _base_character(
            raw,
            name_to_id[raw["name"].casefold()],
            name_to_id,
            opening,
            location_ids,
        )
        for raw in package["characters"]
    ]
    character_by_id = {item["character_id"]: item for item in characters}
    present_ids = [
        name_to_id[name.casefold()]
        for name in _list_of_text(opening.get("present_characters"))
        if name.casefold() in name_to_id
    ]
    if not present_ids:
        present_ids = [item["character_id"] for item in characters if item.get("playable")]
    opening_location_id = location_ids.get(clean_text(opening.get("location")).casefold())

    entity_states = {}
    for character in characters:
        start = character.get("starting_state", {})
        entity_states[character["character_id"]] = {
            "entity_id": character["character_id"],
            "entity_type": "Character",
            "name": character["canonical_name"],
            "record_status": "active_at_scenario_start",
            "mutable_fields": {
                "current_location": character.get("starting_location_id"),
                "current_activity": clean_text(start.get("activity")),
                "mood": clean_text(start.get("mood")),
                "long_term_goal": clean_text(start.get("long_term_goal")),
            },
            "last_updated_by_event_id": None,
        }
    for item in locations:
        if not isinstance(item, dict) or not clean_text(item.get("name")):
            continue
        location_id = location_ids[clean_text(item.get("name")).casefold()]
        entity_states[location_id] = {
            "entity_id": location_id,
            "entity_type": "Location",
            "name": clean_text(item.get("name")),
            "record_status": "active_at_scenario_start",
            "mutable_fields": dict(item.get("starting_state") or {}),
            "last_updated_by_event_id": None,
        }

    relationship_states = {}
    canonical_relationships = []
    for source in characters:
        for rel in source.get("relationships", []):
            rel_id = safe_id(
                source["character_id"]
                + "|"
                + rel["target_entity_id"]
                + "|"
                + rel["relationship_type"],
                "relationship",
            )
            record = {
                "relationship_id": rel_id,
                "participant_ids": [
                    source["character_id"],
                    rel["target_entity_id"],
                ],
                "participant_names": [
                    source["canonical_name"],
                    rel["target_name"],
                ],
                "relationship_type": rel["relationship_type"],
                "current_value": rel["current_state"] or rel["relationship_type"],
                "status": "active",
                "source": "scenario_compilation",
                "evidence_refs": (
                    [{"source_text": rel["evidence"]}] if rel["evidence"] else []
                ),
            }
            relationship_states[rel_id] = record
            canonical_relationships.append(record)

    resource_states = {}
    for character in characters:
        for collection, resource_type in (
            (character.get("abilities", []), "ability"),
            (character.get("owned_items", []), "artifact"),
        ):
            for resource in collection:
                resource_states[resource["entity_id"]] = {
                    "resource_id": resource["entity_id"],
                    "resource_type": resource_type,
                    "canonical_name": resource["canonical_name"],
                    "access_type": "exclusive" if resource_type == "artifact" else "character_bound",
                    "original_owner_ids": [character["character_id"]],
                    "canonical_owner_ids": [character["character_id"]],
                    "current_owner_ids": [character["character_id"]],
                    "current_user_ids": [character["character_id"]],
                    "current_holder_ids": (
                        [character["character_id"]]
                        if resource_type == "artifact"
                        else []
                    ),
                    "status": "available",
                    "last_updated_by_event_id": None,
                }

    fingerprint_seed = {
        "template": template,
        "background_prompt": background_prompt,
        "package": package,
    }
    source_fingerprint = stable_hash(fingerprint_seed)
    current_world_state = {
        "entity_states": entity_states,
        "resource_states": resource_states,
        "relationship_states": relationship_states,
        "identity_states": {},
        "knowledge_ledger": {
            item["character_id"]: item.get("knowledge_scope", [])
            for item in characters
        },
    }
    simulation_template = {
        "schema_version": "1.0",
        "layer": "Simulation State Template",
        "purpose": "Initial state compiled from a reusable scenario project.",
        "source_canonical_db_fingerprint": source_fingerprint,
        "cutoff_order": 1,
        "cutoff_policy": {
            "source_mode": "scenario",
            "opening_is_pressure_not_script": True,
        },
        "current_world_state": current_world_state,
        "known_fact_policy": {
            "character_knowledge_is_scoped": True,
            "unknown_research_claims_remain_unknown": True,
        },
        "available_cutoff_orders": [1],
    }
    simulation_template["simulation_state_template_fingerprint"] = stable_hash(
        simulation_template
    )
    opening_event_id = safe_id(package["title"] + "|opening", "event")
    timeline_db = {
        "schema_version": "1.0",
        "layer": "Canonical Timeline",
        "purpose": "Scenario opening anchor; it is pressure, not a fixed plot.",
        "ordered_event_ids": [opening_event_id],
        "timeline_nodes": [
            {
                "timeline_node_id": "timeline_" + opening_event_id,
                "event_id": opening_event_id,
                "canonical_order": 1,
                "branchable": True,
                "can_be_blocked": True,
                "can_be_altered": True,
                "node_kind": "scenario_opening",
                "state_change_refs": {},
            }
        ],
        "policy": {"canonical_is_default_route_not_destiny": True},
    }
    opening_event = {
        "event_id": opening_event_id,
        "canonical_name": clean_text(opening.get("title")) or "开局",
        "event_type": "scenario_opening",
        "canonical_order": 1,
        "status_in_source": "initial_condition",
        "participants": [
            {
                "entity_id": item,
                "name": character_by_id[item]["canonical_name"],
                "role": "present",
            }
            for item in present_ids
            if item in character_by_id
        ],
        "locations": (
            [
                {
                    "entity_id": opening_location_id,
                    "name": clean_text(opening.get("location")),
                    "role": "setting",
                }
            ]
            if opening_location_id
            else []
        ),
        "preconditions": [],
        "trigger_conditions": [
            {"description": clean_text(item)}
            for item in _list_of_text(opening.get("active_pressures"))
        ],
        "state_changes": [],
        "outcomes": [
            {"description": clean_text(opening.get("summary"))}
        ],
        "can_be_blocked": True,
        "can_be_altered": True,
        "alternative_runtime_hooks": [
            {"hook_type": "unresolved_question", "description": clean_text(item)}
            for item in _list_of_text(opening.get("unresolved_questions"))
        ],
        "source_chunk_ids": ["1"],
        "evidence_refs": [
            {
                "source_chunk_id": 1,
                "source_text": clean_text(opening.get("summary")),
            }
        ],
    }
    event_db = {
        "schema_version": "1.0",
        "layer": "Canonical Events",
        "purpose": "Scenario seed events.",
        "events": {opening_event_id: opening_event},
        "event_order": [opening_event_id],
        "policy": {"runtime_can_diverge": True},
    }
    location_db = {
        "schema_version": "1.0",
        "layer": "Canonical Locations",
        "locations": [
            {
                "entity_id": location_ids[clean_text(item.get("name")).casefold()],
                "canonical_name": clean_text(item.get("name")),
                "description": clean_text(item.get("description")),
                "attributes": item.get("starting_state") or {},
                "source_chunk_ids": [1],
            }
            for item in locations
            if isinstance(item, dict) and clean_text(item.get("name"))
        ],
    }
    compiled_rules = []
    for index, item in enumerate(package["world"].get("rules", []), 1):
        if isinstance(item, dict):
            name = clean_text(item.get("name")) or f"World rule {index}"
            description = clean_text(item.get("description"))
            source = clean_text(item.get("source")) or "scenario_background"
        else:
            name = f"World rule {index}"
            description = clean_text(item)
            source = "scenario_background"
        if not description:
            continue
        compiled_rules.append(
            {
                "rule_id": safe_id(name, "rule"),
                "canonical_name": name,
                "description": description,
                "applies_to": [],
                "constrains": [],
                "model_status": (
                    "trusted" if source == "template" else "supported"
                ),
                "enforcement": "requires_runtime_review",
                "evidence": [
                    {"source": source, "source_text": description}
                ],
            }
        )
    rule_db = {
        "schema_version": "1.0",
        "layer": "Canonical World Rules",
        "rules": compiled_rules,
        "narrative_rules": package.get("narrative_rules", []),
    }
    all_character_names = [item["canonical_name"] for item in characters]
    common_tags = [
        package["title"],
        clean_text(opening.get("title")),
        *all_character_names,
    ]
    knowledge_units = []
    knowledge_ids = set()

    def add_knowledge(
        name,
        descriptions,
        *,
        category,
        model_status,
        source,
        source_url="",
        confidence="",
        tags=None,
    ):
        descriptions = _list_of_text(descriptions)
        name = clean_text(name)
        if not name or not descriptions:
            return
        knowledge_id = safe_id(name + "|" + source + "|" + source_url + "|" + stable_hash(descriptions), "knowledge")
        if knowledge_id in knowledge_ids:
            return
        knowledge_ids.add(knowledge_id)
        knowledge_units.append(
            {
                "knowledge_id": knowledge_id,
                "name": name,
                "canonical_name": name,
                "category": category,
                "descriptions": descriptions,
                "retrieval_tags": list(
                    dict.fromkeys([*common_tags, *(tags or [])])
                ),
                "model_status": model_status,
                "source_url": clean_text(source_url),
                "confidence": clean_text(confidence),
                "source": source,
            }
        )

    for item in package.get("research_notes", []):
        if isinstance(item, dict) and clean_text(item.get("claim")):
            add_knowledge(
                item.get("claim"),
                [item.get("claim")],
                category="researched_background",
                model_status="supported",
                source="scenario_research_note",
                source_url=item.get("source_url"),
                confidence=item.get("confidence") or "medium",
            )
    for item in research_cards or []:
        if isinstance(item, dict) and clean_text(item.get("summary")):
            add_knowledge(
                item.get("title") or "检索资料",
                [item.get("summary")],
                category="retrieved_background",
                model_status="supported",
                source="scenario_research_card",
                source_url=item.get("source_url"),
                confidence="source_summary",
                tags=[item.get("title", "")],
            )
    background_snapshot = None
    if background_knowledge:
        from background_knowledge import background_cards
        background_snapshot = {key: background_knowledge[key] for key in (
            "background_id", "topic", "revision", "updated_at", "entities", "facts", "relationships")}
        for key in ("aliases", "identity", "unknowns", "coverage", "policy", "character_coverage", "reference_quotes"):
            if key in background_knowledge:
                background_snapshot[key] = background_knowledge[key]
        background_snapshot["sources"] = [{key: row[key] for key in (
            "source_id", "title", "source_url", "retrieved_at", "official", "narrative_only", "entity_name")
            if key in row} for row in background_knowledge["sources"]]
        for card in background_cards(background_knowledge):
            if card.get("canon_scope") == "variant":
                continue
            descriptions = [card["summary"], "原文证据：" + card["evidence_quote"]]
            if card.get("as_of"):
                descriptions.append("适用时间/版本：" + card["as_of"])
            if card.get("evidence_section"):
                descriptions.append("原文剧情章节：" + card["evidence_section"])
            if card.get("aliases"):
                descriptions.append("实体别名：" + " / ".join(card["aliases"]))
            if card.get("facets"):
                descriptions.append("人物资料类型：" + " / ".join(card["facets"]))
            if card.get("voice_line"):
                descriptions.append("语言样例（不是历史事件）：" + card["voice_line"] + "；情境：" + card.get("line_context", ""))
            add_knowledge(card["title"], descriptions,
                category="retrieved_background", model_status="supported", source="background_library",
                source_url=card["source_url"], confidence="source_quote", tags=[card["title"], *card.get("aliases", [])])
        source_urls = {row["source_id"]: row["source_url"] for row in background_knowledge["sources"]}
        for row in background_knowledge.get("reference_quotes", []):
            add_knowledge("背景原文检索线索", [row["quote"], "未做事实语义审查，不是确定设定；不得推断剧情规则或人物关系。"],
                category="background_reference", model_status="uncertain", source="background_reference",
                source_url=source_urls[row["source_id"]], confidence="unreviewed_quote")
    if framework_text:
        add_knowledge(
            template.get("name") or "情景模板",
            [framework_text],
            category="user_authored_scenario_framework",
            model_status="trusted",
            source="scenario_template",
        )
    for agent_kind, directives in package.get(
        "world_agent_directives", {}
    ).items():
        for index, directive in enumerate(_list_of_text(directives), 1):
            add_knowledge(
                f"{agent_kind} directive {index}",
                [directive],
                category="user_authored_world_directive",
                model_status="trusted",
                source="scenario_template",
                tags=[agent_kind],
            )
    agent_profiles = []
    for character, raw in zip(characters, package["characters"]):
        profile = {
            "agent_id": "agent_" + character["character_id"],
            "character_id": character["character_id"],
            "canonical_name": character["canonical_name"],
            "playable": bool(raw.get("playable")),
            "profile_tier": "full" if raw.get("playable") else "light",
            "runtime_mode": "scenario_agent",
            "simulation_status": character["simulation_status"],
            "background_summary": character["background_summary"],
            "personality": character["personality"],
            "goals": character["goals"],
            "constraints": character["constraints"],
            "identity": {
                "canonical_name": character["canonical_name"],
                "aliases": character.get("aliases", []),
                "titles": [],
                "forms": [],
                "temporary_identities": [],
                "canonical_identity_names": [
                    character["canonical_name"],
                    *character.get("aliases", []),
                ],
            },
            "identity_layers": {
                "original_identity": clean_text(raw.get("original_identity")),
                "scenario_role": clean_text(raw.get("role")),
                "role_slot_id": clean_text(raw.get("role_slot_id")),
                "template_role": clean_text(raw.get("template_role")),
            },
            "core_motivation": character["core_motivation"],
            "state": {
                "background_summary": character["background_summary"],
                "background_evidence": [],
                "personality": character["personality"],
                "goals": character["goals"],
                "constraints": character["constraints"],
                "speech_styles": (raw.get("portrayal") or {}).get("speech_style", []) if isinstance(raw.get("portrayal"), dict) else [],
                "portrayal": raw.get("portrayal", {}) if isinstance(raw.get("portrayal"), dict) else {},
                "scenario_portrayal": raw.get("scenario_portrayal", {}) if isinstance(raw.get("scenario_portrayal"), dict) else {},
                "canon_evidence": raw.get("canon_evidence", []),
                "knowledge_scope": character["knowledge_scope"],
                "starting_state": character["starting_state"],
            },
            "attributes": character["attributes"],
            "capabilities": {
                "abilities": character["abilities"],
                "owned_items": character["owned_items"],
                "used_items": [],
            },
            "relationships": character["relationships"],
            "weak_relation_candidates": [],
            "metadata_relation_candidates": [],
            "world_context": {
                "knowledge_refs": [
                    item["knowledge_id"] for item in knowledge_units
                ],
                "supported_retrieval_candidates": knowledge_units,
                "scenario_framework": clean_text(template.get("framework_text")),
                "narrative_rules": package.get("narrative_rules", []),
                "role_slot_id": clean_text(raw.get("role_slot_id")),
                "template_role": clean_text(raw.get("template_role")),
                "world_agent_directives": package.get(
                    "world_agent_directives", {}
                ),
            },
            "evidence_refs": [],
            "guardrails": {
                "source_mode": "scenario",
                "unknown_fields_must_remain_unknown": True,
                "runtime_may_diverge": True,
            },
            "retrieval_tags": [
                character["canonical_name"],
                *character.get("aliases", []),
                clean_text(raw.get("role")),
            ],
        }
        profile["profile_fingerprint"] = stable_hash(profile)
        agent_profiles.append(profile)
    agent_db = {
        "schema_version": "1.0",
        "step": "scenario_compile",
        "purpose": "Scenario agent profile templates.",
        "source_character_db_fingerprint": stable_hash(characters),
        "source_world_db_fingerprint": source_fingerprint,
        "project_language": package["language"],
        "identity_policy": {"one_agent_per_character_id": True},
        "agent_count": len(agent_profiles),
        "tier_counts": {
            "full": sum(item["profile_tier"] == "full" for item in agent_profiles),
            "light": sum(item["profile_tier"] == "light" for item in agent_profiles),
        },
        "agents": agent_profiles,
        "reference_only_identities": [],
        "validation": {"valid": True},
    }
    agent_db["agent_profile_db_fingerprint"] = stable_hash(agent_db)
    character_db = {
        "schema_version": "1.0",
        "layer": "Canonical Characters",
        "purpose": "Characters compiled from a scenario project.",
        "characters": characters,
        "character_by_id": character_by_id,
        "policy": {
            "source_mode": "scenario",
            "profiles_are_templates": True,
        },
    }
    character_db["db_fingerprint"] = stable_hash(character_db)
    relationship_db = {
        "schema_version": "1.0",
        "layer": "Canonical Relationships",
        "relationships": canonical_relationships,
        "policy": {"runtime_relationships_are_mutable": True},
    }
    runtime_event_db = {
        "schema_version": "1.0",
        "layer": "Runtime Events",
        "event_queue": [],
        "committed_events": [],
    }
    runtime_relationship_db = {
        "schema_version": "1.0",
        "layer": "Runtime Relationships",
        "relationships": relationship_states,
        "change_log": [],
    }
    runtime_log = {
        "schema_version": "1.0",
        "layer": "Runtime Log",
        "entries": [],
        "archived_entry_count": 0,
    }
    world_db = {
        "schema_version": "1.0",
        "step": "scenario_compile",
        "purpose": "Source-neutral world compiled from a reusable scenario.",
        "source_mode": "scenario",
        "project_language": package["language"],
        "world_sections": {
            "summary": package["world"].get("summary", ""),
            "premise": package["premise"],
            "template_name": template.get("name", ""),
        },
        "knowledge_units": knowledge_units,
        "concept_registry": {},
        "character_identity_index": {
            item["canonical_name"]: item["character_id"] for item in characters
        },
        "timeline": timeline_db["timeline_nodes"],
        "world_state": current_world_state,
        "event_ledger": event_db["events"],
        "rule_engine": {"rules": compiled_rules},
        "runtime_contracts": {
            "canonical_is_read_only": True,
            "runtime_is_current_truth": True,
        },
        "explicit_constraints": package.get("narrative_rules", []),
        "canonical_timeline_db": timeline_db,
        "canonical_event_db": event_db,
        "canonical_character_db": character_db,
        "canonical_relationship_db": relationship_db,
        "canonical_location_db": location_db,
        "canonical_world_rule_db": rule_db,
        "canonical_knowledge_db": {
            "schema_version": "1.0",
            "layer": "Canonical Knowledge",
            "knowledge_units": knowledge_units,
        },
        "simulation_state_db": simulation_template,
        "simulation_state_template": simulation_template,
        "runtime_event_db": runtime_event_db,
        "runtime_relationship_db": runtime_relationship_db,
        "runtime_log": runtime_log,
        "scenario_package": package,
        "scenario_template": template,
        "background_knowledge": background_snapshot,
        "background_knowledge_status": background_notice,
    }
    world_db["knowledge_layers"] = {
        "baseline": {"background_id": (background_snapshot or {}).get("background_id"),
            "revision": (background_snapshot or {}).get("revision"), "fingerprint": stable_hash(background_snapshot),
            "read_only_during_simulation": True},
        "scenario": {"project_id": project_id, "scope": "only_this_project"},
        "runtime": {"scope": "only_this_save", "write_back_to_baseline": False},
    }
    world_db["world_db_fingerprint"] = stable_hash(world_db)

    files = {
        canonical_dir / "world_db.json": world_db,
        canonical_dir / "canonical_timeline_db.json": timeline_db,
        canonical_dir / "canonical_event_db.json": event_db,
        canonical_dir / "canonical_character_db.json": character_db,
        canonical_dir / "canonical_relationship_db.json": relationship_db,
        canonical_dir / "canonical_location_db.json": location_db,
        canonical_dir / "canonical_world_rule_db.json": rule_db,
        canonical_dir / "canonical_knowledge_db.json": world_db[
            "canonical_knowledge_db"
        ],
        agents_dir / "agent_profiles.json": agent_db,
        runtime_dir / "simulation_state_template.json": simulation_template,
        runtime_dir / "runtime_event_db.json": runtime_event_db,
        runtime_dir / "runtime_relationship_db.json": runtime_relationship_db,
        runtime_dir / "runtime_log.json": runtime_log,
        graph_dir / "raw_graph_triples.json": {
            "schema_version": "1.0",
            "source_mode": "scenario",
            "results": [],
            "completed_chunk_count": 0,
            "available_source_chunk_count": 0,
        },
    }
    for path, value in files.items():
        _write_json(path, value)
    if background_snapshot:
        _write_json(graph_dir / "background_knowledge.json", background_snapshot)

    project = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "project_id": project_id,
        "name": clean_text(project_name) or package["title"],
        "source_mode": "scenario",
        "template_id": template.get("template_id"),
        "template_name": template.get("name"),
        "background_prompt": clean_text(background_prompt),
        "research_cards": research_cards or [],
        "scenario_package": package,
        "db_root": str(db_root),
        "background_knowledge": background_snapshot,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    _write_json(root / "project.json", project)
    return project


def reset_project_runtime(project_id):
    project = load_project(project_id)
    if not project:
        raise FileNotFoundError(project_id)
    state = Path(project["db_root"]) / "runtime" / "simulation_state.json"
    if state.is_file():
        backup = state.with_suffix(
            ".json." + utc_now().replace(":", "-") + ".bak"
        )
        shutil.move(str(state), str(backup))
        return backup
    return None


def project_required_files(db_root):
    db_root = Path(db_root)
    rows = [
        ("canonical/world_db.json", "世界数据库"),
        ("canonical/canonical_character_db.json", "角色数据库"),
        ("canonical/canonical_timeline_db.json", "开局时间锚点"),
        ("canonical/canonical_event_db.json", "开局事件"),
        ("agents/agent_profiles.json", "Agent Profiles"),
        ("runtime/simulation_state_template.json", "初始状态"),
        ("runtime/runtime_event_db.json", "运行时事件"),
        ("runtime/runtime_relationship_db.json", "运行时关系"),
        ("runtime/runtime_log.json", "运行时日志"),
    ]
    return [
        {
            "name": name,
            "description": description,
            "path": db_root / name,
            "exists": (db_root / name).is_file(),
        }
        for name, description in rows
    ]
