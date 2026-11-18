// Vietnamese names of the pipeline's codes.

export const MENTION_TYPES: Record<string, string> = {
  entity: "Thực thể",
  token: "Token",
  class: "Nhóm thực thể",
  address: "Địa chỉ có nhãn",
  literal_address: "Địa chỉ trong câu",
  nil: "Tên không có trong KB",
  property: "Thuộc tính",
  time: "Thời gian",
  unsupported: "Ngoài phạm vi dữ liệu",
};

export const ERROR_KINDS: Record<string, string> = {
  no_sql: "Không có SQL trong câu trả lời",
  unknown_placeholder: "Placeholder không có trong evidence",
  parse: "SQLGlot không parse được (DuckDB)",
  not_a_query: "Không phải một câu SELECT",
  execution: "Lỗi khi chạy trên DuckDB",
  timeout: "Quá thời gian chạy (30 s)",
  missing_placeholder: "Bỏ sót placeholder (model tự viết địa chỉ)",
};

export const EX_KINDS: Record<string, string> = {
  correct: "Đúng",
  wrong_result: "Sai kết quả",
  extra_columns: "Đúng nhưng thừa cột",
  abstained: "Từ chối nhầm",
  wrong_decision: "Quyết định sai",
  missing: "Không có dự đoán",
  skipped: "Prompt quá dài",
  ...ERROR_KINDS,
};

export const DECISIONS: Record<string, { title: string; body: string }> = {
  nil: {
    title: "Không nhận ra tên trong câu hỏi",
    body: "Linker không tìm thấy tên này trong cơ sở tri thức, nên hệ thống không sinh SQL thay vì đoán địa chỉ.",
  },
  unsupported: {
    title: "Dữ liệu không trả lời được câu này",
    body: "Câu hỏi cần thông tin nằm ngoài các bảng của tháng 9/2026 (ví dụ số dư ví, giao dịch nội bộ), nên hệ thống từ chối trả lời.",
  },
  skipped: {
    title: "Prompt dài hơn ngữ cảnh của model",
    body: "Prompt cộng phần trả lời vượt quá 8.192 token nên không được gửi tới model.",
  },
};

export const EXPECTED: Record<string, string> = {
  sql: "Có SQL",
  nil: "Phải từ chối (nil)",
  unsupported: "Phải từ chối (unsupported)",
};

export const LANGS: Record<string, string> = { vi: "Tiếng Việt", en: "English" };
