// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/// Math utility module providing basic mathematical operations

/// Add two integers
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Multiply two integers
pub fn multiply(a: i32, b: i32) -> i32 {
    a * b
}

/// Calculate factorial of a number
pub fn factorial(n: u32) -> u64 {
    if n <= 1 {
        return 1;
    }
    let mut result = 1u64;
    for i in 2..=n {
        result *= i as u64;
    }
    result
}

/// Check if a number is prime
pub fn is_prime(n: i32) -> bool {
    if n <= 1 {
        return false;
    }
    if n <= 3 {
        return true;
    }
    if n % 2 == 0 || n % 3 == 0 {
        return false;
    }

    let mut i = 5;
    while i * i <= n {
        if n % i == 0 || n % (i + 2) == 0 {
            return false;
        }
        i += 6;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_add() {
        assert_eq!(add(5, 3), 8);
        assert_eq!(add(-2, 2), 0);
    }

    #[test]
    fn test_multiply() {
        assert_eq!(multiply(4, 7), 28);
        assert_eq!(multiply(-3, 5), -15);
    }

    #[test]
    fn test_factorial() {
        assert_eq!(factorial(0), 1);
        assert_eq!(factorial(5), 120);
        assert_eq!(factorial(10), 3628800);
    }

    #[test]
    fn test_is_prime() {
        assert_eq!(is_prime(2), true);
        assert_eq!(is_prime(17), true);
        assert_eq!(is_prime(4), false);
        assert_eq!(is_prime(1), false);
    }
}
