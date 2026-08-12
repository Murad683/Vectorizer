"""Document assembly: combine classified elements into finished files.

Consumes the ordered ``list[ClassifiedElement]`` from the classifier and
the existing vision → layout → shape-renderer chain; produces one
complete output document per format, elements in source order.
"""

from app.core.assembly.docx_assembler import assemble_docx
from app.core.assembly.pptx_assembler import assemble_pptx

__all__ = ["assemble_docx", "assemble_pptx"]
