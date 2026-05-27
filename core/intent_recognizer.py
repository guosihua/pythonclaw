"""
意图识别模块 - 通过大模型进行意图识别和技能选择

功能：
1. 调用大模型进行意图分类
2. 根据意图选择合适的技能
3. 实现健壮的输出解析（JSON优先→正则抽取→白名单校验）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Tuple

from .llm.base import LLMProvider

logger = logging.getLogger(__name__)


class IntentRecognizer:
    """
    通过大模型进行意图识别和技能选择
    """

    def __init__(self, llm_provider: LLMProvider):
        self.llm_provider = llm_provider

    def recognize_intent(self, user_input: str, available_skills: list) -> Tuple[str, str]:
        """
        通过大模型识别用户意图并选择技能
        
        Args:
            user_input: 用户输入文本
            available_skills: 可用技能列表（SkillMetadata 对象列表）
        
        Returns:
            (intent, skill_name): 识别到的意图和选择的技能名称
        """
        if not available_skills:
            logger.warning("[IntentRecognizer] No skills available")
            return "unknown", None

        # 构建技能信息
        skill_info = self._build_skill_info(available_skills)
        logger.info(f"[IntentRecognizer] User input: {user_input}")
        logger.info(f"[IntentRecognizer] Available skills: {[s.name for s in available_skills]}")
        
        # 构建 Prompt
        prompt = self._build_intent_prompt(user_input, skill_info)
        
        # 打印完整提示词便于排查
        system_prompt = self._get_system_prompt()
        logger.info(f"[IntentRecognizer] System prompt:\n{system_prompt}")
        logger.info(f"[IntentRecognizer] User prompt:\n{prompt}")
        
        # 调用大模型
        try:
            logger.info(f"[IntentRecognizer] Calling LLM for intent recognition...")
            response = self.llm_provider.chat(
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            logger.info(f"[IntentRecognizer] LLM call successful, parsing response...")
            
            # 解析响应
            result = self._parse_llm_response(response, available_skills)
            logger.info(f"[IntentRecognizer] Final result: intent={result[0]}, skill={result[1]}")
            return result
            
        except Exception as e:
            logger.error(f"[IntentRecognizer] LLM intent recognition failed: {e}")
            return "unknown", None

    def _build_skill_info(self, available_skills: list) -> str:
        """构建技能信息字符串"""
        skill_items = []
        for skill in available_skills:
            name = getattr(skill, 'name', 'unknown')
            description = getattr(skill, 'description', '')
            skill_items.append(f"- {name}: {description}")
        
        return "\n".join(skill_items)

    def _get_system_prompt(self) -> str:
        """获取系统提示词"""
        return """
你是一个专业的网络故障排查意图分类器。

## 任务要求
根据用户输入，识别其意图并选择最适合的技能。

## 输出格式
你必须严格按照以下 JSON 格式输出，只输出 JSON，不输出任何其他内容：
{"intent": "<意图类型>", "skill": "<技能名称>"}

## 意图类型定义
- troubleshooting: 当用户询问"故障排查"、"诊断"、"排查"、"静态路由故障排查"、"以太口故障排查"、"端口不通"、"网络不通"等时，选择此意图
- information: 当用户询问"查询"、"获取"、"显示"配置或状态信息时，选择此意图
- configuration: 当用户要求"配置"、"修改"、"设置"网络设备参数时，选择此意图
- unknown: 当无法确定用户意图时，选择此意图

## 响应要求
- 只输出 JSON 格式，不要输出任何解释、思考过程或其他内容
- JSON 必须可以被 json.loads() 直接解析
"""

    def _build_intent_prompt(self, user_input: str, skill_info: str) -> str:
        """构建意图识别的用户提示词，动态包含技能名称和描述信息"""
        output_format = '{"intent": "<意图类型>", "skill": "<技能名称>"}'
        return f"""
用户输入：{user_input}

可用技能列表及其描述（name: description）：
{skill_info}

## 技能选择规则
1. 如果用户输入包含"故障排查"、"排查"、"诊断"等词，意图选择 troubleshooting
2. 根据用户输入的具体内容，选择描述最匹配的技能
3. 如果没有匹配的技能，返回空字符串 "" 作为 skill

请根据用户输入，识别意图并选择最合适的技能。

输出格式：
{output_format}
"""

    def _parse_llm_response(self, response, available_skills: list) -> Tuple[str, str]:
        """
        解析大模型响应，采用三段式解析策略：
        1. JSON优先解析
        2. 正则抽取（支持多种格式）
        3. 基于关键词的兜底提取
        """
        try:
            # 获取响应内容
            if hasattr(response, 'choices') and response.choices:
                message = response.choices[0].message
                content = message.content if hasattr(message, 'content') else str(message)
            else:
                content = str(response)

            # 打印完整的大模型响应便于排查
            logger.info(f"[IntentRecognizer] LLM response (full):\n{content}")

            # 阶段1: JSON优先解析
            try:
                parsed = json.loads(content)
                intent = parsed.get("intent", "unknown")
                skill = parsed.get("skill", "")
                logger.info(f"[IntentRecognizer] JSON parsed - intent: {intent}, skill: {skill}")
                # 即使技能不在列表中，也返回意图（技能选择会在后面处理）
                if self._validate_skill(skill, available_skills):
                    logger.info(f"[IntentRecognizer] JSON解析成功 - 意图: {intent}, 技能: {skill}")
                    return intent, skill
                else:
                    # 技能不在列表中，但意图是有效的
                    if intent and intent != "unknown":
                        logger.info(f"[IntentRecognizer] JSON解析成功，但技能不在列表中 - 意图: {intent}, 技能: {skill}")
                        return intent, skill
            except json.JSONDecodeError:
                logger.debug("[IntentRecognizer] JSON解析失败，尝试正则抽取")

            # 阶段2: 正则抽取 - 支持多种格式
            patterns = [
                # 标准JSON格式: "intent": "xxx", "skill": "xxx"
                (r'"intent"\s*[：:]\s*["\']([^"\']+)["\']', r'"skill"\s*[：:]\s*["\']([^"\']+)["\']'),
                # 中文引号格式: "意图": "xxx", "技能": "xxx"
                (r'"意图"\s*[：:]\s*["\']([^"\']+)["\']', r'"技能"\s*[：:]\s*["\']([^"\']+)["\']'),
                # 解释文本中的格式: intent可以设为"xxx", skill设为"xxx"
                (r'intent[可以设为合适选择]*[：:\s]*["\']([^"\']+)["\']', r'skill[可以设为合适选择]*[：:\s]*["\']([^"\']+)["\']'),
                # 直接提取 troubleshooting 和 static-troubleshooting
                (r'troubleshooting', r'static-troubleshooting'),
            ]

            for intent_pattern, skill_pattern in patterns:
                intent_match = re.search(intent_pattern, content, re.IGNORECASE)
                skill_match = re.search(skill_pattern, content, re.IGNORECASE)
                
                if intent_match and skill_match:
                    intent = intent_match.group(1) if intent_match.lastindex else intent_match.group(0)
                    skill = skill_match.group(1) if skill_match.lastindex else skill_match.group(0)
                    logger.info(f"[IntentRecognizer] 正则抽取成功 - 意图: {intent}, 技能: {skill}")
                    
                    # 白名单校验
                    if self._validate_skill(skill, available_skills):
                        return intent, skill
                    # 即使技能校验失败，也尝试返回有效意图
                    if 'troubleshoot' in intent.lower():
                        return 'troubleshooting', skill

            # 阶段3: 基于关键词的兜底提取
            # 检查是否提到了 troubleshooting 相关内容
            if 'troubleshoot' in content.lower() or '故障排查' in content:
                # 查找最可能的技能
                if 'static' in content.lower() and 'static-troubleshooting' in str([s.name for s in available_skills]):
                    return 'troubleshooting', 'static-troubleshooting'
                if 'ethernet' in content.lower() or '端口' in content or '接口' in content:
                    if 'ethernet-port-troubleshooting' in str([s.name for s in available_skills]):
                        return 'troubleshooting', 'ethernet-port-troubleshooting'

            logger.warning(f"[IntentRecognizer] 无法从响应中提取有效意图和技能")

        except Exception as e:
            logger.error(f"[IntentRecognizer] 解析响应失败: {e}")

        return "unknown", None

    def _validate_skill(self, skill_name: str, available_skills: list) -> bool:
        """校验技能名称是否在可用列表中"""
        if not skill_name:
            return False
        
        available_names = [getattr(s, 'name', '') for s in available_skills]
        return skill_name in available_names
