# Helpers execute inside an ordinary managed job. No pager or terminal input is used.
proc ::innovus_mcp::help_receipt {metadata} {
    variable job_dir
    set path [file join $job_dir help.meta]
    set channel [open ${path}.tmp w]
    try {
        dict set metadata format_version 1
        dict for {key value} $metadata {
            puts $channel "$key=[binary encode base64 -maxlen 0 [encoding convertto utf-8 $value]]"
        }
    } finally {
        close $channel
    }
    file rename -force ${path}.tmp $path
}

proc ::innovus_mcp::help_exec {args} {
    variable job_dir
    set diagnostics [file join $job_dir help_stderr.txt]
    # Redirect stderr explicitly: Tcl exec otherwise treats warnings as execution errors.
    set code [catch {exec -- {*}$args 2> $diagnostics} text options]
    set channel [open $diagnostics r]
    try {set stderr [read $channel 2048]} finally {close $channel}
    set exit_code 0
    if {$code} {
        set exit_code -1
        if {[dict exists $options -errorcode]} {
            set error_code [dict get $options -errorcode]
            if {[lindex $error_code 0] eq "CHILDSTATUS"} {
                set exit_code [lindex $error_code 2]
            }
        }
    }
    return [dict create code $code text $text stderr $stderr exit_code $exit_code]
}

proc ::innovus_mcp::command_help {command detail} {
    set metadata [dict create command $command requested_detail $detail detail $detail \
        status unavailable complete 0 source innovus_help source_path "" \
        innovus_version unknown documentation_version unknown ui_mode unknown \
        body_artifact output external_exit_code "" message "" diagnostics ""]
    if {![catch {uplevel #0 {get_db program_version}} version]} {
        dict set metadata innovus_version $version
    }
    if {![catch {uplevel #0 {is_common_ui_mode}} mode]} {
        dict set metadata ui_mode [expr {$mode ? "stylus" : "legacy"}]
    }
    if {$detail eq "full"} {
        set failure "System man is unavailable"
        set man [auto_execok man]
        if {$man ne ""} {
            # Resolve with the target Innovus process's MANPATH, then render that exact file.
            set resolved [::innovus_mcp::help_exec {*}$man -w -- $command]
            dict set metadata external_exit_code [dict get $resolved exit_code]
            dict set metadata diagnostics [dict get $resolved stderr]
            set failure [dict get $resolved text]
            if {![dict get $resolved code]} {
                set source [lindex [split [dict get $resolved text] \n] 0]
                dict set metadata source_path $source
                set doc_mode unknown
                if {[string match */share/innovus/man/* $source]} {set doc_mode legacy}
                if {[string match */share/innovus/stylus/man/* $source]} {set doc_mode stylus}
                set expected_mode [dict get $metadata ui_mode]
                set failure "Manual source could not be matched to the target Innovus UI"
                if {$doc_mode ne "unknown" && $expected_mode eq $doc_mode && [file isfile $source]} {
                    set rendered [::innovus_mcp::help_exec {*}$man -P cat -l -- $source]
                    dict set metadata external_exit_code [dict get $rendered exit_code]
                    dict set metadata diagnostics "[dict get $resolved stderr][dict get $rendered stderr]"
                    set body [dict get $rendered text]
                    set failure $body
                    if {![dict get $rendered code] && [string trim $body] ne ""} {
                        if {[regexp {Product Version[ \t]+([^ \t\r\n]+)} $body -> doc_version]} {
                            dict set metadata documentation_version $doc_version
                        }
                        set actual [dict get $metadata innovus_version]
                        set documented [dict get $metadata documentation_version]
                        if {$actual ne "unknown" && $documented ne "unknown" &&
                            ![string match ${documented}* $actual]} {
                            set failure "Manual version $documented differs from Innovus $actual"
                        } else {
                            dict set metadata source system_man_noninteractive
                            dict set metadata body_artifact result
                            dict set metadata status ok
                            dict set metadata complete 1
                            ::innovus_mcp::help_receipt $metadata
                            return $body
                        }
                    }
                }
            }
        }
        # An unavailable full manual degrades to runtime help, never to an interactive pager.
        dict set metadata message [string range "Full manual unavailable: $failure" 0 2047]
        dict set metadata detail summary
    }
    set known [uplevel #0 [list namespace which -command $command]]
    if {$known eq ""} {
        dict set metadata message "[dict get $metadata message] Unknown command: $command"
        ::innovus_mcp::help_receipt $metadata
        return ""
    }
    set code [catch {uplevel #0 [list help $command]} body options]
    if {!$code} {
        dict set metadata status [expr {$detail eq "summary" ? "ok" : "fallback"}]
        dict set metadata complete [expr {$detail eq "summary"}]
        if {$body ne ""} {dict set metadata body_artifact result}
    } else {
        dict set metadata message "[dict get $metadata message] Runtime help failed: $body"
    }
    ::innovus_mcp::help_receipt $metadata
    return $body
}
