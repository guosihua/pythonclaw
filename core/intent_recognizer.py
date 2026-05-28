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
你是一个专业的技能匹配助手。

## 任务要求
根据用户输入，从可用技能列表中选择最匹配的一个。

## 输出格式
只输出一个JSON对象，不要输出任何其他内容：
{"skill": "CT-ac-feiwlanganlao-troubleshooting"}

注意：必须将技能名称放在引号内，直接输出JSON对象。

## 响应要求
- 只输出JSON格式，不要输出任何解释、思考过程或其他内容
- JSON必须可以被json.loads()直接解析
- 如果没有匹配的技能，输出 {"skill": ""}
"""

    def _build_intent_prompt(self, user_input: str, skill_info: str) -> str:
        """构建意图识别的用户提示词，动态包含技能名称和描述信息"""
        return f"""
用户输入：{user_input}

可用技能列表及其描述（name: description）：
{skill_info}

## 技能选择规则
根据用户输入的具体内容，从可用技能列表中选择描述最匹配的技能。

请直接输出JSON格式的技能名称，例如：{{"skill": "ethernet-port-troubleshooting"}}
"""

    def _parse_llm_response(self, response, available_skills: list) -> Tuple[str, str]:
        """
        解析大模型响应，采用两段式解析策略：
        1. JSON优先解析
        2. 正则抽取
        """
        try:
            # 获取响应内容
            if hasattr(response, 'choices') and response.choices:
                message = response.choices[0].message
                content = message.content if hasattr(message, 'content') else str(message)
            else:
                content = str(response)

            # 打印完整的大模型响应便于排查
            logger.info(f"[IntentRecognizer] ==================== LLM Response Start ====================")
            logger.info(f"[IntentRecognizer] LLM原始输出:\n{content}")
            logger.info(f"[IntentRecognizer] ==================== LLM Response End ====================")

            # 阶段1: JSON优先解析 - 增强容错性
            logger.info(f"[IntentRecognizer] 阶段1: 尝试JSON解析...")
            try:
                # 尝试多种JSON解析方式
                parsed = None

                # 方式1: 直接解析
                try:
                    parsed = json.loads(content.strip())
                    logger.info(f"[IntentRecognizer] 方式1直接解析成功")
                except json.JSONDecodeError:
                    logger.debug("[IntentRecognizer] 方式1直接解析失败，尝试方式2")

                # 方式2: 提取所有 {"skill": "xxx"} 格式的JSON，取最后一个（通常LLM最后输出的才是正确答案）
                if parsed is None:
                    json_patterns = [
                        r'\{"skill"\s*:\s*"([^"]+)"\}',  # {"skill": "xxx"}
                        r'\{"skill"\s*:\s*\'([^\']+)\'\}',  # {"skill": 'xxx'}
                    ]
                    best_match = None
                    for pattern in json_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            # 取最后一个匹配（通常是最终输出）
                            best_match = matches[-1]
                            logger.info(f"[IntentRecognizer] 方式2找到匹配: {matches}, 使用最后一个: {best_match}")
                            break

                    if best_match:
                        try:
                            test_json = '{"skill": "' + best_match + '"}'
                            parsed = json.loads(test_json)
                            logger.info(f"[IntentRecognizer] 方式2正则提取JSON成功: {test_json}")
                        except Exception as e:
                            logger.debug(f"[IntentRecognizer] 方式2正则提取JSON失败: {e}")

                if parsed:
                    skill = parsed.get("skill", "")
                    logger.info(f"[IntentRecognizer] JSON解析结果: skill='{skill}'")
                    if self._validate_skill(skill, available_skills):
                        logger.info(f"[IntentRecognizer] ✓ JSON解析成功，技能验证通过: {skill}")
                        return "troubleshooting", skill
                    else:
                        logger.warning(f"[IntentRecognizer] ✗ JSON解析成功，但技能不在列表中: {skill}")
                        logger.info(f"[IntentRecognizer] 可用技能列表: {[s.name for s in available_skills]}")
                        return "troubleshooting", skill
                else:
                    logger.debug("[IntentRecognizer] 无法从响应中提取JSON")

            except Exception as e:
                logger.error(f"[IntentRecognizer] JSON解析异常: {e}")

            # 阶段2: 穷举法 - 提取所有可能的技能名称，逐一验证
            logger.info(f"[IntentRecognizer] 阶段2: 穷举法提取所有可能技能...")
            available_names = [getattr(s, 'name', '') for s in available_skills]
            logger.info(f"[IntentRecognizer] 可用技能白名单: {available_names}")

            # 提取所有引号内的值，逐一检查是否在技能列表中
            all_quoted = re.findall(r'"([^"]+)"', content)
            logger.info(f"[IntentRecognizer] 所有引号内容: {all_quoted}")

            # 从后往前找（通常LLM最后输出的才是正确答案）
            for candidate in reversed(all_quoted):
                if candidate in available_names:
                    logger.info(f"[IntentRecognizer] ✓ 穷举法找到有效技能: {candidate}")
                    return "troubleshooting", candidate

            logger.warning(f"[IntentRecognizer] ✗ 无法从响应中提取有效技能")

        except Exception as e:
            logger.error(f"[IntentRecognizer] 解析响应失败: {e}")

        return "unknown", None

    def _validate_skill(self, skill_name: str, available_skills: list) -> bool:
        """校验技能名称是否在可用列表中"""
        if not skill_name:
            return False
        
        available_names = [getattr(s, 'name', '') for s in available_skills]
        return skill_name in available_names
