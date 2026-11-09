import re
from typing import List, Dict, Any, Optional
from app.config import settings
from app.models.document import ChunkItem, SourceLocation, SectionNode

def estimate_token_count(text: str) -> int:
    """
    Robust token estimator supporting Korean Hangul and English/code.
    - Hangul syllables (~0.7 tokens per char, or 1 token per ~1.5 chars)
    - English words (~1.3 tokens per word)
    - Punctuation & symbols
    """
    if not text:
        return 0

    hangul_chars = len(re.findall(r"[\uac00-\ud7a3]", text))
    other_chars = len(text) - hangul_chars

    # Approx 1 token per 1.5 Korean chars, approx 1 token per 3.5 non-Korean chars
    korean_tokens = hangul_chars / 1.5
    other_tokens = other_chars / 3.5
    total = int(korean_tokens + other_tokens)
    return max(1, total)

class ChunkingEngine:
    def __init__(
        self,
        target_tokens: int = settings.CHUNK_TARGET_TOKENS,
        max_tokens: int = settings.CHUNK_MAX_TOKENS,
        overlap_tokens: int = settings.CHUNK_OVERLAP_TOKENS
    ):
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def chunk_section(
        self,
        document_id: str,
        section_id: str,
        title: str,
        content: str,
        source: SourceLocation,
        figure_ids: List[str],
        start_chunk_idx: int = 1
    ) -> List[ChunkItem]:
        """
        Splits section content into token-bounded chunks while preserving context,
        paragraph integrity, and table boundaries.
        """
        clean_text = content.strip()
        if not clean_text:
            # If section only has a title and figures
            text = title if title else "Empty Section"
            return [ChunkItem(
                chunk_id=f"chk_{start_chunk_idx:04d}",
                document_id=document_id,
                parent_section_id=section_id,
                content=text,
                figure_ids=figure_ids,
                source=source,
                token_count=estimate_token_count(text)
            )]

        total_tokens = estimate_token_count(clean_text)
        if total_tokens <= self.max_tokens:
            return [ChunkItem(
                chunk_id=f"chk_{start_chunk_idx:04d}",
                document_id=document_id,
                parent_section_id=section_id,
                content=clean_text,
                figure_ids=figure_ids,
                source=source,
                token_count=total_tokens
            )]

        # Split into blocks (paragraphs or table blocks)
        blocks = self._split_into_blocks(clean_text)
        chunks: List[ChunkItem] = []
        current_chunk_blocks: List[str] = []
        current_tokens = 0
        chunk_idx = start_chunk_idx

        for block in blocks:
            block_tokens = estimate_token_count(block)
            
            # If a single block is giant (larger than max_tokens), split by sentences
            if block_tokens > self.max_tokens:
                sub_blocks = self._split_giant_block(block, self.target_tokens)
                for sb in sub_blocks:
                    sb_tokens = estimate_token_count(sb)
                    if current_tokens + sb_tokens > self.max_tokens and current_chunk_blocks:
                        chunks.append(self._create_chunk(
                            chunk_idx, document_id, section_id, current_chunk_blocks, figure_ids, source
                        ))
                        chunk_idx += 1
                        current_chunk_blocks = []
                        current_tokens = 0
                    current_chunk_blocks.append(sb)
                    current_tokens += sb_tokens
                continue

            if current_tokens + block_tokens > self.target_tokens and current_chunk_blocks:
                # Flush current chunk
                chunks.append(self._create_chunk(
                    chunk_idx, document_id, section_id, current_chunk_blocks, figure_ids, source
                ))
                chunk_idx += 1
                
                # Apply overlap from tail of current chunk if possible
                overlap_text = self._get_overlap_text(current_chunk_blocks, self.overlap_tokens)
                current_chunk_blocks = [overlap_text] if overlap_text else []
                current_tokens = estimate_token_count(overlap_text) if overlap_text else 0

            current_chunk_blocks.append(block)
            current_tokens += block_tokens

        if current_chunk_blocks:
            chunks.append(self._create_chunk(
                chunk_idx, document_id, section_id, current_chunk_blocks, figure_ids, source
            ))

        return chunks

    def _split_into_blocks(self, text: str) -> List[str]:
        """Separates text into paragraphs, keeping markdown tables intact."""
        lines = text.splitlines()
        blocks = []
        cur_lines = []
        in_table = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|"):
                in_table = True
                cur_lines.append(line)
            else:
                if in_table:
                    in_table = False
                    blocks.append("\n".join(cur_lines))
                    cur_lines = []
                if not stripped:
                    if cur_lines:
                        blocks.append("\n".join(cur_lines))
                        cur_lines = []
                else:
                    cur_lines.append(line)

        if cur_lines:
            blocks.append("\n".join(cur_lines))

        return [b for b in blocks if b.strip()]

    def _split_giant_block(self, block: str, target_tokens: int) -> List[str]:
        """Splits an oversized block by sentences or punctuation."""
        sentences = re.split(r"(?<=[.?!])\s+", block)
        res = []
        cur = []
        cur_tok = 0
        for s in sentences:
            s_tok = estimate_token_count(s)
            if cur_tok + s_tok > target_tokens and cur:
                res.append(" ".join(cur))
                cur = []
                cur_tok = 0
            cur.append(s)
            cur_tok += s_tok
        if cur:
            res.append(" ".join(cur))
        return res

    def _get_overlap_text(self, blocks: List[str], overlap_tokens: int) -> str:
        """Picks trailing text to provide context for the next chunk."""
        if not blocks or overlap_tokens <= 0:
            return ""
        candidate = blocks[-1]
        tokens = estimate_token_count(candidate)
        if tokens <= overlap_tokens:
            return candidate
        # Shorten to approx tokens
        words = candidate.split()
        if len(words) > 20:
            return " ".join(words[-20:])
        return candidate

    def _create_chunk(
        self,
        idx: int,
        doc_id: str,
        sec_id: str,
        blocks: List[str],
        figure_ids: List[str],
        source: SourceLocation
    ) -> ChunkItem:
        content = "\n\n".join(blocks).strip()
        tokens = estimate_token_count(content)
        return ChunkItem(
            chunk_id=f"chk_{idx:04d}",
            document_id=doc_id,
            parent_section_id=sec_id,
            content=content,
            figure_ids=figure_ids,
            source=source,
            token_count=tokens
        )
