# =====================================================
# preprocess.R
# Read locomotor Excel → metadata preview + timed counts
# Does NOT ask for user parameters and does NOT detect episodes.
# =====================================================

# Expected packages: readxl, dplyr, tidyr, lubridate, stringr, tibble

# ---- Helpers ----

cell_chr <- function(x) {
  if (length(x) == 0 || is.null(x) || (length(x) == 1 && is.na(x))) {
    return(NA_character_)
  }
  x <- x[[1]]
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(NA_character_)
  stringr::str_squish(as.character(x))
}

is_blank_chr <- function(x) {
  is.na(x) || !nzchar(x) || tolower(x) %in% c("na", "null", "none")
}

is_summary_label <- function(s) {
  !is.na(s) && tolower(s) %in% c("dark", "light", "ratio", "cage")
}

# Excel stores times as day-fractions (0.795... = 19:05).
# After midnight, values often become 1.003..., 1.006..., etc.
# readxl frequently returns these as character strings. IMPORTANT:
# do NOT parse "1.25" as clock H:M — that was a prior bug (1:25).
excel_serial_to_sec <- function(num) {
  if (is.na(num) || num < 0) return(NA_real_)
  round((num %% 1) * 86400) %% 86400
}

to_sec_of_day <- function(x, tz_out = "UTC") {
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(NA_real_)
  x <- x[[1]]

  if (inherits(x, "Period")) {
    return(as.numeric(x, "seconds") %% 86400)
  }
  if (inherits(x, "hms") || inherits(x, "difftime")) {
    return(as.numeric(x) %% 86400)
  }
  if (inherits(x, "POSIXt") || inherits(x, "Date")) {
    x <- as.POSIXct(x, tz = tz_out)
    return(lubridate::hour(x) * 3600 + lubridate::minute(x) * 60 + lubridate::second(x))
  }
  if (is.numeric(x) && !is.na(x)) {
    return(excel_serial_to_sec(x))
  }

  s <- cell_chr(x)
  if (is_blank_chr(s) || is_summary_label(s)) return(NA_real_)

  num <- suppressWarnings(as.numeric(s))
  if (!is.na(num) && num >= 0) {
    return(excel_serial_to_sec(num))
  }

  parsed <- lubridate::parse_date_time(
    toupper(stringr::str_replace_all(s, "\u00A0", " ")),
    orders = c("I:M p", "I:M%p", "I:M:%S p", "I:M:S p",
               "H:M:S", "H:M", "Ymd HMS", "Ymd HM"),
    tz = tz_out,
    exact = FALSE,
    quiet = TRUE
  )
  if (is.na(parsed)) return(NA_real_)
  lubridate::hour(parsed) * 3600 +
    lubridate::minute(parsed) * 60 +
    lubridate::second(parsed)
}

extract_date <- function(x, tz_out = "UTC") {
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(as.Date(NA))
  x <- x[[1]]
  if (inherits(x, "Date")) return(x)
  if (inherits(x, "POSIXt")) return(as.Date(x, tz = tz_out))
  if (is.numeric(x) && !is.na(x)) {
    if (x > 1e9 && x < 2e10) {
      return(as.Date(as.POSIXct(x, origin = "1970-01-01", tz = tz_out), tz = tz_out))
    }
    if (x > 20000 && x < 100000) {
      return(as.Date(x, origin = "1899-12-30"))
    }
  }
  s <- cell_chr(x)
  if (is_blank_chr(s)) return(as.Date(NA))
  num <- suppressWarnings(as.numeric(s))
  if (!is.na(num)) return(extract_date(num, tz_out = tz_out))
  parsed <- suppressWarnings(lubridate::parse_date_time(
    s, orders = c("Ymd", "mdy", "dmy"), quiet = TRUE
  ))
  if (is.na(parsed)) return(as.Date(NA))
  as.Date(parsed)
}

fmt_clock <- function(sec) {
  sec <- round(sec) %% 86400
  sprintf("%02d:%02d:%02d", sec %/% 3600, (sec %% 3600) %/% 60, sec %% 60)
}

statistical_mode <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) == 0) return(NA_real_)
  ux <- unique(x)
  ux[which.max(tabulate(match(x, ux)))]
}

col_to_letter <- function(n) {
  out <- character(length(n))
  for (i in seq_along(n)) {
    x <- n[i]
    s <- ""
    while (x > 0) {
      r <- (x - 1) %% 26
      s <- paste0(LETTERS[r + 1], s)
      x <- (x - 1) %/% 26
    }
    out[i] <- s
  }
  out
}

resolve_input_xlsx <- function(input_xlsx = NULL) {
  if (!is.null(input_xlsx) && nzchar(input_xlsx)) return(input_xlsx)
  candidates <- list.files(pattern = "\\.xlsx$", full.names = TRUE)
  candidates <- candidates[!grepl(
    "wake_episodes|sample_file|~\\$",
    basename(candidates),
    ignore.case = TRUE
  )]
  if (length(candidates) == 0) stop("No .xlsx file found in working directory.")
  candidates[[1]]
}

#' Preprocess a locomotor activity Excel file.
#'
#' @return A list with animal_meta, keep_secs, counts, bin_minutes,
#'   suggested_start_date, and preview fields. No episode detection.
preprocess_locomotor <- function(
  input_xlsx = NULL,
  sheet = 1,
  bin_minutes = NULL,
  start_date_fallback = as.Date("2020-01-01"),
  tz_out = "UTC"
) {
  input_xlsx <- resolve_input_xlsx(input_xlsx)
  if (!file.exists(input_xlsx)) stop("Input file not found: ", input_xlsx)

  message("Reading: ", input_xlsx)
  raw <- readxl::read_excel(
    input_xlsx, sheet = sheet, col_names = FALSE, .name_repair = "minimal"
  )

  n_rows <- nrow(raw)
  n_cols <- ncol(raw)
  if (n_rows < 4) stop("File has fewer than 4 rows; expected metadata + data.")

  col_date <- 1
  col_time <- 2

  # Locate metadata relative to the "cage" label in column B.
  # readxl may drop a leading blank Excel row, so absolute row numbers are unreliable.
  col_b <- vapply(seq_len(n_rows), function(r) {
    s <- cell_chr(raw[[col_time]][r])
    if (is.na(s)) NA_character_ else tolower(s)
  }, character(1))

  row_cage <- which(col_b == "cage")[1]
  if (is.na(row_cage) || row_cage < 2) {
    stop('Could not find "cage" label in column B. Check the Excel layout.')
  }
  row_animal <- row_cage - 1
  row_label  <- row_cage + 1
  row_data0  <- row_cage + 2

  if (row_data0 > n_rows) stop("No data rows found below the metadata block.")

  message(
    "Detected rows — animal #: ", row_animal,
    ", cage: ", row_cage,
    ", label: ", row_label,
    ", data start: ", row_data0
  )

  meta_rows <- list()
  for (c in seq(3, n_cols)) {
    animal_raw <- cell_chr(raw[[c]][row_animal])
    if (is_blank_chr(animal_raw)) next

    animal_id <- suppressWarnings(as.integer(animal_raw))
    if (is.na(animal_id)) {
      warning("Skipping column ", c, ": animal # is not an integer (", animal_raw, ").")
      next
    }

    cage_raw <- cell_chr(raw[[c]][row_cage])
    if (!is.na(cage_raw) && tolower(cage_raw) == "cage") cage_raw <- NA_character_

    label_raw <- cell_chr(raw[[c]][row_label])
    if (is_blank_chr(label_raw)) label_raw <- NA_character_
    if (is_blank_chr(cage_raw)) cage_raw <- NA_character_

    meta_rows[[length(meta_rows) + 1]] <- tibble::tibble(
      excel_col = c,
      excel_col_letter = col_to_letter(c),
      animal_id = animal_id,
      cage_code = cage_raw,
      animal_label = label_raw,
      column_name = paste0("animal_", animal_id)
    )
  }

  animal_meta <- dplyr::bind_rows(meta_rows)
  if (nrow(animal_meta) == 0) {
    stop("No animal columns found. Expected integer animal # values in the animal row, columns C+.")
  }
  animal_meta <- dplyr::select(
    animal_meta,
    excel_col_letter, animal_id, cage_code, animal_label, column_name, excel_col
  )

  # Contiguous time block; stop at first blank / summary / unparseable time
  keep_rows <- integer(0)
  keep_secs <- numeric(0)
  for (r in seq(row_data0, n_rows)) {
    sec <- to_sec_of_day(raw[[col_time]][r], tz_out = tz_out)
    if (is.na(sec)) break
    keep_rows <- c(keep_rows, r)
    keep_secs <- c(keep_secs, sec)
  }
  if (length(keep_rows) == 0) {
    stop("No parseable time values found in column B from the data start row onward.")
  }

  sec_diffs <- diff(keep_secs)
  pos_diffs_min <- round(sec_diffs[sec_diffs > 0] / 60)
  detected_bin <- statistical_mode(pos_diffs_min)

  if (is.null(bin_minutes) || is.na(bin_minutes)) {
    if (is.na(detected_bin) || detected_bin <= 0) {
      stop("Could not auto-detect bin size. Set bin_minutes manually.")
    }
    bin_minutes <- as.integer(detected_bin)
  } else {
    bin_minutes <- as.integer(bin_minutes)
  }

  suggested_start_date <- extract_date(raw[[col_date]][row_data0], tz_out = tz_out)
  if (is.na(suggested_start_date)) suggested_start_date <- as.Date(start_date_fallback)

  # Count matrix aligned to keep_secs (no time_fixed yet — date chosen later)
  count_list <- list()
  for (i in seq_len(nrow(animal_meta))) {
    c <- animal_meta$excel_col[i]
    nm <- animal_meta$column_name[i]
    vals <- vapply(keep_rows, function(r) {
      v <- suppressWarnings(as.numeric(cell_chr(raw[[c]][r])))
      if (length(v) == 0) NA_real_ else v
    }, numeric(1))
    count_list[[nm]] <- vals
  }
  counts <- tibble::as_tibble(count_list)

  n_wraps <- sum(c(FALSE, keep_secs[-1] < keep_secs[-length(keep_secs)]))

  message("\n===== Preprocess summary =====")
  message("File: ", basename(input_xlsx))
  message("Suggested start date: ", suggested_start_date)
  message("Detected bin size: ", bin_minutes, " minutes")
  message("Rows (bins) per animal: ", length(keep_rows))
  message("First clock time: ", fmt_clock(keep_secs[1]))
  message("Last clock time:  ", fmt_clock(keep_secs[length(keep_secs)]))
  message("Midnight crossings (clock wrap): ", n_wraps)
  message("\nAnimal metadata:")
  print(
    dplyr::select(animal_meta, excel_col_letter, animal_id, cage_code, animal_label),
    n = Inf
  )

  list(
    input_xlsx = input_xlsx,
    animal_meta = animal_meta,
    keep_secs = keep_secs,
    counts = counts,
    bin_minutes = bin_minutes,
    suggested_start_date = suggested_start_date,
    n_bins = length(keep_rows),
    first_clock = fmt_clock(keep_secs[1]),
    last_clock = fmt_clock(keep_secs[length(keep_secs)]),
    n_midnight_wraps = n_wraps,
    tz_out = tz_out
  )
}
