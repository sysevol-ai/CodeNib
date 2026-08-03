/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef MATH_UTILS_H
#define MATH_UTILS_H

namespace MathUtils {

/**
 * Add two integers
 * @param a First integer
 * @param b Second integer
 * @return Sum of a and b
 */
int add(int a, int b);

/**
 * Multiply two integers
 * @param a First integer
 * @param b Second integer
 * @return Product of a and b
 */
int multiply(int a, int b);

/**
 * Calculate factorial of a number
 * @param n The number
 * @return Factorial of n
 */
long long factorial(int n);

/**
 * Check if a number is prime
 * @param n The number to check
 * @return true if n is prime, false otherwise
 */
bool isPrime(int n);

} // namespace MathUtils

#endif // MATH_UTILS_H
