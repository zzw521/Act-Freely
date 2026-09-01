// ═══════════════════════════════════════════════════════════
// src/main/file/convert.ts — 文件格式转换管道
// ═══════════════════════════════════════════════════════════

import fs from 'fs';
import path from 'path';
import Logger from '../util/logger';

// ── Types ─────────────────────────────────────────────────

export type FileFormat = 'docx' | 'xlsx' | 'pdf' | 'csv' | 'txt' | 'md';

export interface ConvertOptions {
  inputPath: string;
  outputPath: string;
  inputFormat: FileFormat;
  outputFormat: FileFormat;
}

export interface ConvertResult {
  success: boolean;
  outputPath: string;
  error?: string;
  durationMs: number;
}

// ── Intermediate Representation ───────────────────────────

export interface IRDocument {
  type: 'document';
  metadata: {
    title?: string;
    author?: string;
    createdAt?: string;
  };
  content: IRContent[];
}

export type IRContent = IRParagraph | IRTable | IRHeading | IRList | IRCodeBlock;

export interface IRParagraph {
  type: 'paragraph';
  text: string;
  formatting?: {
    bold?: boolean;
    italic?: boolean;
  };
}

export interface IRHeading {
  type: 'heading';
  level: number;
  text: string;
}

export interface IRTable {
  type: 'table';
  headers: string[];
  rows: string[][];
}

export interface IRList {
  type: 'list';
  ordered: boolean;
  items: string[];
}

export interface IRCodeBlock {
  type: 'code';
  language?: string;
  code: string;
}

// ── Format Converter ──────────────────────────────────────

export class FormatConverter {
  private static instance: FormatConverter | null = null;
  private logger = Logger.getInstance();

  static getInstance(): FormatConverter {
    if (!FormatConverter.instance) {
      FormatConverter.instance = new FormatConverter();
    }
    return FormatConverter.instance;
  }

  static resetInstance(): void {
    FormatConverter.instance = null;
  }

  // ── Main Conversion Pipeline ──────────────────────────

  async convert(options: ConvertOptions): Promise<ConvertResult> {
    const startTime = Date.now();
    const { inputPath, outputPath, inputFormat, outputFormat } = options;

    this.logger.info('Starting file conversion', {
      module: 'convert',
      inputPath,
      outputPath,
      inputFormat,
      outputFormat,
    });

    try {
      // Validate input file exists
      if (!fs.existsSync(inputPath)) {
        throw new Error(`Input file not found: ${inputPath}`);
      }

      // Short-circuit for same format
      if (inputFormat === outputFormat) {
        fs.copyFileSync(inputPath, outputPath);
        return {
          success: true,
          outputPath,
          durationMs: Date.now() - startTime,
        };
      }

      // Step 1: Parse input to IR
      const ir = await this.parseToIR(inputPath, inputFormat);

      // Step 2: Generate output from IR
      await this.generateFromIR(ir, outputPath, outputFormat);

      const durationMs = Date.now() - startTime;
      this.logger.info('Conversion completed', {
        module: 'convert',
        outputPath,
        durationMs,
      });

      return {
        success: true,
        outputPath,
        durationMs,
      };
    } catch (error: any) {
      this.logger.error('Conversion failed', {
        module: 'convert',
        inputPath,
        error: error.message,
      });

      return {
        success: false,
        outputPath,
        error: error.message,
        durationMs: Date.now() - startTime,
      };
    }
  }

  // ── Parsers (File → IR) ───────────────────────────────

  private async parseToIR(filePath: string, format: FileFormat): Promise<IRDocument> {
    switch (format) {
      case 'txt':
        return this.parseTxt(filePath);
      case 'md':
        return this.parseMd(filePath);
      case 'csv':
        return this.parseCsv(filePath);
      case 'xlsx':
        return this.parseXlsx(filePath);
      case 'docx':
        return this.parseDocx(filePath);
      case 'pdf':
        return this.parsePdf(filePath);
      default:
        throw new Error(`Unsupported input format: ${format}`);
    }
  }

  private async parseTxt(filePath: string): Promise<IRDocument> {
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.split('\n');

    const irContent: IRContent[] = lines.map(line => ({
      type: 'paragraph' as const,
      text: line,
    }));

    return {
      type: 'document',
      metadata: {},
      content: irContent,
    };
  }

  private async parseMd(filePath: string): Promise<IRDocument> {
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.split('\n');
    const irContent: IRContent[] = [];

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      // Headings
      const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
      if (headingMatch) {
        irContent.push({
          type: 'heading',
          level: headingMatch[1].length,
          text: headingMatch[2],
        });
        i++;
        continue;
      }

      // Code blocks
      if (line.startsWith('```')) {
        // Simplified: skip code block content for now
        i++;
        continue;
      }

      // Markdown table: header row starts with '|'
      if (line.trim().startsWith('|') && i + 1 < lines.length) {
        const separator = lines[i + 1];
        if (/^\|\s*[-:]+\s*/.test(separator.trim())) {
          const headers = this.parseMdTableRow(line);
          const rows: string[][] = [];
          let j = i + 2;
          while (j < lines.length && lines[j].trim().startsWith('|')) {
            rows.push(this.parseMdTableRow(lines[j]));
            j++;
          }
          irContent.push({
            type: 'table',
            headers,
            rows,
          });
          i = j;
          continue;
        }
      }

      // Regular paragraphs
      if (line.trim()) {
        irContent.push({
          type: 'paragraph',
          text: line,
        });
      }
      i++;
    }

    return {
      type: 'document',
      metadata: {},
      content: irContent,
    };
  }

  private async parseCsv(filePath: string): Promise<IRDocument> {
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.split('\n').filter(line => line.trim());

    if (lines.length === 0) {
      return { type: 'document', metadata: {}, content: [] };
    }

    // Parse CSV (simple implementation)
    const headers = this.parseCsvLine(lines[0]);
    const rows = lines.slice(1).map(line => this.parseCsvLine(line));

    return {
      type: 'document',
      metadata: {},
      content: [
        {
          type: 'table',
          headers,
          rows,
        },
      ],
    };
  }

  private async parseXlsx(filePath: string): Promise<IRDocument> {
    // TODO: Implement xlsx parsing with SheetJS
    // For now, throw error indicating dependency needed
    throw new Error('XLSX parsing not yet implemented. Requires: xlsx (SheetJS)');
  }

  private async parseDocx(filePath: string): Promise<IRDocument> {
    // TODO: Implement docx parsing with mammoth
    throw new Error('DOCX parsing not yet implemented. Requires: mammoth');
  }

  private async parsePdf(filePath: string): Promise<IRDocument> {
    // TODO: Implement pdf parsing with pdf-parse
    throw new Error('PDF parsing not yet implemented. Requires: pdf-parse');
  }

  // ── Generators (IR → File) ────────────────────────────

  private async generateFromIR(
    ir: IRDocument,
    outputPath: string,
    format: FileFormat,
  ): Promise<void> {
    switch (format) {
      case 'txt':
        return this.generateTxt(ir, outputPath);
      case 'md':
        return this.generateMd(ir, outputPath);
      case 'csv':
        return this.generateCsv(ir, outputPath);
      case 'xlsx':
        return this.generateXlsx(ir, outputPath);
      case 'docx':
        return this.generateDocx(ir, outputPath);
      case 'pdf':
        return this.generatePdf(ir, outputPath);
      default:
        throw new Error(`Unsupported output format: ${format}`);
    }
  }

  private async generateTxt(ir: IRDocument, outputPath: string): Promise<void> {
    const lines: string[] = [];

    for (const item of ir.content) {
      switch (item.type) {
        case 'paragraph':
          lines.push(item.text);
          break;
        case 'heading':
          lines.push(item.text);
          lines.push('='.repeat(item.text.length));
          break;
        case 'table':
          lines.push(item.headers.join('\t'));
          for (const row of item.rows) {
            lines.push(row.join('\t'));
          }
          break;
        case 'list':
          for (const listItem of item.items) {
            lines.push(`- ${listItem}`);
          }
          break;
        case 'code':
          lines.push(item.code);
          break;
      }
    }

    fs.writeFileSync(outputPath, lines.join('\n'), 'utf-8');
  }

  private async generateMd(ir: IRDocument, outputPath: string): Promise<void> {
    const lines: string[] = [];

    for (const item of ir.content) {
      switch (item.type) {
        case 'paragraph':
          let text = item.text;
          if (item.formatting?.bold) text = `**${text}**`;
          if (item.formatting?.italic) text = `*${text}*`;
          lines.push(text);
          break;
        case 'heading':
          lines.push(`${'#'.repeat(item.level)} ${item.text}`);
          break;
        case 'table':
          lines.push(`| ${item.headers.join(' | ')} |`);
          lines.push(`| ${item.headers.map(() => '---').join(' | ')} |`);
          for (const row of item.rows) {
            lines.push(`| ${row.join(' | ')} |`);
          }
          break;
        case 'list':
          for (let i = 0; i < item.items.length; i++) {
            const prefix = item.ordered ? `${i + 1}.` : '-';
            lines.push(`${prefix} ${item.items[i]}`);
          }
          break;
        case 'code':
          lines.push('```' + (item.language || ''));
          lines.push(item.code);
          lines.push('```');
          break;
      }
      lines.push(''); // Empty line between elements
    }

    fs.writeFileSync(outputPath, lines.join('\n'), 'utf-8');
  }

  private async generateCsv(ir: IRDocument, outputPath: string): Promise<void> {
    const lines: string[] = [];

    for (const item of ir.content) {
      if (item.type === 'table') {
        lines.push(this.toCsvLine(item.headers));
        for (const row of item.rows) {
          lines.push(this.toCsvLine(row));
        }
      }
    }

    fs.writeFileSync(outputPath, lines.join('\n'), 'utf-8');
  }

  private async generateXlsx(ir: IRDocument, outputPath: string): Promise<void> {
    // TODO: Implement xlsx generation with SheetJS
    throw new Error('XLSX generation not yet implemented. Requires: xlsx (SheetJS)');
  }

  private async generateDocx(ir: IRDocument, outputPath: string): Promise<void> {
    // TODO: Implement docx generation with mammoth/docx library
    throw new Error('DOCX generation not yet implemented. Requires: docx library');
  }

  private async generatePdf(ir: IRDocument, outputPath: string): Promise<void> {
    // TODO: Implement pdf generation with pdf-lib
    throw new Error('PDF generation not yet implemented. Requires: pdf-lib');
  }

  // ── Helper Methods ────────────────────────────────────

  private parseMdTableRow(line: string): string[] {
    return line
      .trim()
      .replace(/^\|/, '')
      .replace(/\|$/, '')
      .split('|')
      .map(cell => cell.trim());
  }

  private parseCsvLine(line: string): string[] {
    // RFC 4180 compliant CSV parser (handles quoted fields and escaped quotes)
    const result: string[] = [];
    let current = '';
    let inQuotes = false;

    for (let i = 0; i < line.length; i++) {
      const char = line[i];

      if (inQuotes) {
        if (char === '"') {
          // Check for escaped quote (""")
          if (i + 1 < line.length && line[i + 1] === '"') {
            current += '"';
            i++;
          } else {
            inQuotes = false;
          }
        } else {
          current += char;
        }
      } else {
        if (char === '"') {
          inQuotes = true;
        } else if (char === ',') {
          result.push(current.trim());
          current = '';
        } else {
          current += char;
        }
      }
    }

    result.push(current.trim());
    return result;
  }

  private toCsvLine(fields: string[]): string {
    return fields
      .map(field => {
        // Escape quotes and wrap in quotes if contains comma
        const escaped = field.replace(/"/g, '""');
        return field.includes(',') || field.includes('"') ? `"${escaped}"` : field;
      })
      .join(',');
  }

  // ── Supported Formats ─────────────────────────────────

  getSupportedFormats(): FileFormat[] {
    return ['txt', 'md', 'csv', 'xlsx', 'docx', 'pdf'];
  }

  canConvert(inputFormat: FileFormat, outputFormat: FileFormat): boolean {
    const supported = this.getSupportedFormats();
    return supported.includes(inputFormat) && supported.includes(outputFormat);
  }
}

export default FormatConverter;
